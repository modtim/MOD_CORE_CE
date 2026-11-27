"""GS.PSR.2 — DB-backed platform-settings registry.

Tunables and feature flags live in the `platform_settings` table instead
of env (env vars are bootstrap/secrets only — see the "settings-not-env-files"
rule). A typed registry maps each key to its Python type, default,
optional env fallback (migration bridge only), validator, group and
description.

Resolution order (get_setting):
    tenant row (scope='tenant', if tenant_id given)
  > platform row (scope='platform')
  > env_fallback (os.getenv of the named var, coerced to the type)
  > default

Write path (set_setting): type/validator check, then SOFT-SUPERSEDE —
the old live row gets `deleted_at` stamped and a fresh row is inserted,
so no value is ever hard-deleted (ISO 27001). An append-only
`platform_settings_audit` row is written in the same transaction.

Caching: a short-TTL in-process cache of the DB rows (so repeated reads
don't hammer Postgres) plus a Valkey pub/sub invalidation on channel
`platform_settings:invalidate` so every API process drops its cache within
seconds of a change. If Valkey is unreachable we degrade to TTL-only —
setting reads/writes NEVER raise because of the cache layer.

Readers are migrated onto this service incrementally (GS.PSR.6); nothing
else calls into this module yet.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

import models  # AuditAction for banner-sync failure recording
from models import PlatformSetting, PlatformSettingAudit

import audit  # AC-8 banner-sync failure recording (audit/notification seam)
import httpx  # Keycloak admin REST calls (banner sync)

logger = logging.getLogger(__name__)

INVALIDATE_CHANNEL = "platform_settings:invalidate"

# Short TTL: even if a pub/sub message is missed (Valkey blip, slow
# subscriber), a stale value can live at most this long in one process.
CACHE_TTL_SECONDS = 30.0

_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


class SettingNotRegisteredError(KeyError):
    """Raised when get/set is called with a key never passed to register()."""


# ---------------------------------------------------------------------------
# Value coercion (env strings / JSONB round-trip -> registered Python type)
# ---------------------------------------------------------------------------

def _coerce_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = str(raw).strip().lower()
    if s == "":
        # Empty env values mean "off" — matches the legacy
        # `os.getenv(VAR, "").lower() in ("1", "true", ...)` readers.
        return False
    if s in _TRUE_STRINGS:
        return True
    if s in _FALSE_STRINGS:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


def _coerce_int(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError("bool is not a valid int setting value")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw.is_integer():
            return int(raw)
        raise ValueError(f"not an integer: {raw!r}")
    return int(str(raw).strip())


def _coerce_float(raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError("bool is not a valid float setting value")
    if isinstance(raw, (int, float)):
        return float(raw)
    return float(str(raw).strip())


def _coerce_str(raw: Any) -> str:
    return str(raw)


def _coerce_dict(raw: Any) -> dict:
    """A plain dict (JSONB round-trips to dict natively; the value column
    deserializes via models.JSONB). Rejects non-dict values outright —
    coercion is value validation, not repair."""
    if isinstance(raw, dict):
        return raw
    raise ValueError(f"not a dict: {raw!r}")


def _coerce_list(raw: Any) -> list:
    """A plain list (JSONB round-trips to list natively). Tuples
    normalize to lists; everything else is rejected — coercion is value
    validation, not repair. Membership validation (e.g. allowed
    multi-select values) belongs in the spec's ``validator``."""
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, tuple):
        return list(raw)
    raise ValueError(f"not a list: {raw!r}")


_COERCERS: Dict[type, Callable[[Any], Any]] = {
    bool: _coerce_bool,
    int: _coerce_int,
    float: _coerce_float,
    str: _coerce_str,
    dict: _coerce_dict,
    list: _coerce_list,
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _SettingSpec:
    key: str
    type_: type
    default: Any
    env_fallback: Optional[str] = None
    restart_required: bool = False
    validator: Optional[Callable[[Any], None]] = None
    description: str = ""
    group: str = ""
    ce_hidden: bool = False


_REGISTRY: Dict[str, _SettingSpec] = {}


def register(key: str, type_: type, default: Any,
             env_fallback: Optional[str] = None,
             restart_required: bool = False,
             validator: Optional[Callable[[Any], None]] = None,
             description: str = "",
             group: str = "",
             ce_hidden: bool = False) -> None:
    """Declare a setting. Re-registering the same key is a no-op so module
    reloads (hot-reload / double import) don't clobber a spec."""
    if key in _REGISTRY:
        return
    if type_ not in _COERCERS:
        raise TypeError(f"unsupported setting type for {key!r}: {type_!r}")
    _coerce(type_, default)  # the default must itself be a valid value
    _REGISTRY[key] = _SettingSpec(
        key=key,
        type_=type_,
        default=_coerce(type_, default),
        env_fallback=env_fallback,
        restart_required=restart_required,
        validator=validator,
        description=description,
        group=group,
        ce_hidden=ce_hidden,
    )


# ---------------------------------------------------------------------------
# Security-sensitive keys (GSEC)
# ---------------------------------------------------------------------------
# Every change to these keys MUST produce an audit row even when the writer
# is a script: the normal write path (set_setting / clear_setting) already
# appends a `platform_settings_audit` row in the same transaction, so this
# set is the marker audit verification (and any future bypass-detection
# check) reads. A raw INSERT into `platform_settings` that skips set_setting
# is the only way to break the trail — the 2026-10-03 seed of
# licensing.trust_local_issuer did exactly that, and
# migrations/20261004h_audit_trust_local_issuer_seed.py backfills its row.
# Values recorded for these keys are boolean switches, never secrets, so
# old/new values are stored as-is (secret-looking values go through
# utils.secret_redaction elsewhere).
SECURITY_SENSITIVE_KEYS = frozenset({
    "licensing.trust_local_issuer",
})


def is_security_sensitive_key(key: str) -> bool:
    return key in SECURITY_SENSITIVE_KEYS


def _coerce(type_: type, raw: Any) -> Any:
    return _COERCERS[type_](raw)


# ---------------------------------------------------------------------------
# CE-hidden registry entries (owner answer 75, 11-13)
# ---------------------------------------------------------------------------
# A handful of registry entries exist ONLY to serve hosted billing (plan
# grace windows, plan-downgrade overage restrictions). Their descriptions
# name plans / tiers / seats, which is paid wording CE must not surface in
# the admin settings page. Marking is declarative (`ce_hidden=True` on the
# register() call); the ONLY thing that changes is what the admin LIST and
# GET surfaces return. The keys stay registered on every edition, so
# get_setting/resolve_source/set_setting, the seed, the evidence/support
# bundles and paid behaviour are byte-identical.
def _ce_hidden_probe() -> bool:
    """True iff this is CE. Same fail-to-PAID shape as _ce_unlimited: any
    error from the edition probe means PAID, i.e. nothing is hidden."""
    try:
        from utils.paid_import import is_ce_edition
        return bool(is_ce_edition())
    except Exception:  # noqa: BLE001 — fail to PAID (show everything)
        return False


# ---------------------------------------------------------------------------
# PRO-FEATURE keys hidden on CE (owner 11-15, Q-B3-PLATFORM-SETTINGS: A)
# ---------------------------------------------------------------------------
# The 'System > Settings > Platform settings' subtab STAYS in CE (CE needs it
# to set tunables without env vars), but keys that configure a PRO / hosted
# (paid) feature are not listed there. This is the LIST + GET surface ONLY:
# every key below stays registered on every edition, so get_setting /
# resolve_source / set_setting / the seed / the evidence bundles and paid
# behaviour are byte-identical. A CE write to a hidden key is still accepted
# (the write path is untouched); the key is simply never surfaced.
# One literal name per line, each with the feature it configures, so the
# choice is reviewable. UNSURE keys stay visible (listed in the result file).
PRO_FEATURE_KEYS_HIDDEN_ON_CE = frozenset({
    # hosted billing / subscription lifecycle (Stripe checkout, plan tiers)
    "billing.cancellation_lifecycle_enabled",   # hosted cancel lifecycle
    "billing.record_retention_years",           # invoice/payment record shredding
    "billing.retention_job_enabled",            # the nightly billing-record job
    "billing_basis",                            # run costing vs the plan estimate
    "relay_metering_enabled",                   # per-tenant MOD-DERP relay metering
    # hosted signup + paid checkout sanctions gate
    "sanctions.enabled",                        # sanctioned-country checkout gate
    "sanctions.blocked_countries",
    "sanctions.sdn_path",
    "sanctions.screening_enabled",
    # trial / payment fraud controls (Stripe card fingerprints, credit spend)
    "fraud.one_trial_per_card",
    "fraud.new_tenant_gpu_spend_limit_usd",
    # hosted self-serve sign-up attestation
    "signup.age_attestation_required",
    "signup.minimum_age",
    # node marketplace: try-before-you-buy + listing review + payout
    "node_trial_rate_limit_per_hour",
    "marketplace.report_autohold_threshold",
    "marketplace.mod_built_only_enforced",
    # enterprise support tiers (24/7 owner rotation, Sev-1 paging)
    "support_tickets.response_targets",
    "oncall.sev1_ticket_paging_enabled",
    # enterprise dedicated-GPU isolation (hosted cloud only, owner Q63=A)
    "gpu.dedicated",                          # = DEDICATED_GPU_SETTING_KEY
    "worker.gpu_tenant_scrub",
    # the ONE platform-level log stream a SaaS install may use
    "log_streams.platform_stream_enabled",
    # paid LLM quotas (CE runs them UNLIMITED; the quota keys are paid knobs)
    "llm.monthly_token_quota",
    "llm.window_max_calls",
    # per-PLAN rate limits (no plans exist in CE)
    "api.rate_limit.plan_limits",
    "api.rate_limit.default_rpm",
    # mesh / multi-region control plane (paid mesh routers are carve-denied)
    "mesh.acl_enforced",
    "mesh.preauth_key_ttl_minutes",
    "mesh.public_control_plane",
    # STACK-CALL-MESH-RELAY: the relay container is not shipped in CE at all
    # (services/mesh_relay.py and services/mesh_acl.py are both api_allow-denied),
    # so its two tunables configure a feature CE does not have.
    "mesh.relay_enabled",
    "mesh.relay_tag",
    # compliance profiles / environments are paid (owner Q157, CARVE-U9a)
    "compliance.profiles",
    "compliance.declared_environment",
    "compliance.disruptive_grace_days",
    "compliance.grace_deadlines",
    "compliance.export_control_enforced",
    "compliance.cui_requires_us_person",
    "compliance.itar_requires_us_residency",
    "compliance.withheld_stuck_minutes",
    "compliance.watermark_downloads_enabled",
    "compliance.watermark_min_label",
    "compliance.watermark_unsupported_action",
    "compliance.data_marking_requires_us_person",
    # SSO/SCIM requirement family: every reader of these two keys is denied
    # from the CE extract (check 8373e3f2a4), so the settings do nothing in
    # CE — hidden from the Platform settings subtab, still resolvable.
    "auth.require_sso",                     # per-tenant require-SSO login gate
    "auth.sso_requirement_enabled",         # global kill switch for that gate
    # telemetry payload egress knobs: CE ships its own defaults in
    # services/telemetry_payload.py, so the admin-facing rows are paid knobs.
    "egress.telemetry_enabled",             # login-page RUM beacon ingestion
    "telemetry.usage_stats",                # anonymous usage-stats egress
    "telemetry.update_check",               # version/update-check egress
    # D74 residual (DIAG 89cfc85ce1, owner 11-15 Q-B3 A): billing / licence /
    # fraud rows that name a paid feature and still showed on the CE admin page.
    "billing.meter_sampling_enabled",       # paid billing meter sampling
    "billing.payg_setup_gate_enabled",      # PAYG payment-setup gate
    "licence.readonly_on_expiry",           # licence-expiry read-only mode
    "license.expiry_warn_days",             # licence expiry banner
    "license.last_known_state",             # licence state cache
    "licensing.trust_local_issuer",         # licence issuer trust
    "egress.license_phonehome_enabled",     # licence-server call
    "fraud.block_disposable_email",         # hosted sign-up abuse control
    "fraud.disposable_email_domains",       # hosted sign-up abuse control
    "fraud.mining_detection_enabled",       # hosted GPU-mining detection
    "fraud.mining_egress_gb_threshold",
    "fraud.mining_gpu_sustained_minutes",
    "fraud.mining_sweep_interval_minutes",
    "fraud.new_tenant_gpu_concurrency_limit",  # new-tenant payment-history cap
    "models.commercial_safe_only",          # hosted subscription plan filter
    "usage.concurrent_processes_reconcile_interval_seconds",  # usage enforcement
})


def is_ce_hidden(key: str) -> bool:
    """True when `key` is hidden on CE: either marked `ce_hidden=True` on its
    register() call (owner 75, 11-13) or listed in
    PRO_FEATURE_KEYS_HIDDEN_ON_CE above (owner 11-15 Q-B3-PLATFORM-SETTINGS: A).
    Both are LIST/GET surface only; a broken edition probe falls back to PAID
    (nothing hidden)."""
    spec = _REGISTRY.get(key)
    if spec is None:
        return False
    if not (getattr(spec, "ce_hidden", False)
            or key in PRO_FEATURE_KEYS_HIDDEN_ON_CE):
        return False
    return _ce_hidden_probe()


def _spec(key: str) -> _SettingSpec:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise SettingNotRegisteredError(
            f"platform setting {key!r} is not registered — call "
            f"register() before get/set"
        ) from None


# ---------------------------------------------------------------------------
# In-process cache + Valkey pub/sub invalidation
# ---------------------------------------------------------------------------
#
# Cache maps (key, scope, tenant_id) -> (value, expires_at). Only the DB
# rows are cached (env/default resolution is a trivial in-process lookup,
# re-evaluated fresh on every call so an env change is never masked).
#
# The cache layer must NEVER raise: a Valkey outage degrades to TTL-only.

_cache: Dict[Tuple[str, str, Optional[int]], Tuple[Any, float]] = {}
_cache_lock = threading.Lock()
_subscriber_started = False
_subscriber_lock = threading.Lock()


def _cache_get(key: str, scope: str, tenant_id: Optional[int]):
    entry = _cache.get((key, scope, tenant_id))
    if entry is None:
        return None, False
    value, expires_at = entry
    if time.monotonic() >= expires_at:
        with _cache_lock:
            _cache.pop((key, scope, tenant_id), None)
        return None, False
    return value, True


def _cache_put(key: str, scope: str, tenant_id: Optional[int], value: Any) -> None:
    with _cache_lock:
        _cache[(key, scope, tenant_id)] = (value, time.monotonic() + CACHE_TTL_SECONDS)


def _purge_all() -> None:
    with _cache_lock:
        _cache.clear()


def _purge_key(key: str, tenant_id: Optional[int]) -> None:
    with _cache_lock:
        _cache.pop((key, "tenant", tenant_id), None)
        _cache.pop((key, "platform", None), None)


def _redis_client():
    """Lazy import + connect (mirrors utils/describe_cache._client_factory)
    so tests can monkeypatch this to a fake without a live broker."""
    import redis  # local import — pytest can pre-mock the module
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)


def _publish_invalidate() -> None:
    """Broadcast the invalidation. Best-effort: if Valkey is down we just
    log and let the TTL expire the cache instead of raising."""
    try:
        _redis_client().publish(INVALIDATE_CHANNEL, b"1")
    except Exception as e:  # noqa: BLE001 — degrade to TTL-only, never raise
        logger.debug("platform_settings: invalidate publish failed "
                     "(TTL-only until next success): %s", e)


def _subscribe_loop() -> None:
    """Reconnect-forever subscriber. Every message purges the local cache.
    All failures (connect, read) back off and retry — the loop never
    propagates an exception and the thread is a daemon, so a dead Valkey
    only costs us the TTL."""
    while True:
        try:
            client = _redis_client()
            pubsub = client.pubsub()
            pubsub.subscribe(INVALIDATE_CHANNEL)
            _purge_all()  # resync on (re)connect — we may have missed writes
            for _msg in pubsub.listen():
                _purge_all()
        except Exception as e:  # noqa: BLE001
            logger.debug("platform_settings: subscriber down, retrying: %s", e)
            time.sleep(2.0)


def _ensure_subscriber() -> None:
    global _subscriber_started
    if _subscriber_started:
        return
    with _subscriber_lock:
        if _subscriber_started:
            return
        threading.Thread(
            target=_subscribe_loop,
            name="platform-settings-invalidation",
            daemon=True,
        ).start()
        _subscriber_started = True


def _disable_subscriber() -> None:
    """Test hook: stop the lazy subscriber from spawning (the loop thread,
    if already running, is a daemon and harmless)."""
    global _subscriber_started
    _subscriber_started = True


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def _lookup_row(db, key: str, tenant_id: Optional[int], scope: str) -> Optional[Any]:
    """The live DB value for (key, scope, tenant) or None. Cached with a
    short TTL; `.astext` is never used — the JSONB column deserializes to
    a plain Python value via the models.JSONB TypeDecorator."""
    cached, hit = _cache_get(key, scope, tenant_id)
    if hit:
        return cached

    query = db.query(PlatformSetting.value).filter(
        PlatformSetting.key == key,
        PlatformSetting.scope == scope,
        PlatformSetting.deleted_at.is_(None),
    )
    if scope == "tenant":
        query = query.filter(PlatformSetting.tenant_id == tenant_id)
    else:
        query = query.filter(PlatformSetting.tenant_id.is_(None))
    value = query.scalar()
    if value is not None:
        _cache_put(key, scope, tenant_id, value)
    return value


def get_setting(key: str, db=None, tenant_id: Optional[int] = None) -> Any:
    """Resolve a setting: tenant row > platform row > env fallback > default.

    `db=None` opens and closes a short session of its own (the same
    `database.SessionLocal` pattern other services use); pass a session to
    participate in the caller's transaction."""
    spec = _spec(key)
    _ensure_subscriber()

    own_session = db is None
    if own_session:
        from database import SessionLocal
        db = SessionLocal()
    try:
        if tenant_id is not None:
            value = _lookup_row(db, key, tenant_id, "tenant")
            if value is not None:
                return _coerce(spec.type_, value)
        value = _lookup_row(db, key, None, "platform")
        if value is not None:
            return _coerce(spec.type_, value)
    finally:
        if own_session:
            db.close()

    if spec.env_fallback is not None:
        raw = os.getenv(spec.env_fallback)
        if raw is not None:
            return _coerce(spec.type_, raw)
    return spec.default


def resolve_source(key: str, db, tenant_id: Optional[int] = None) -> Tuple[Any, str]:
    """(value, source) with source in {'db', 'env', 'default'} — the
    per-key rows the future admin settings page renders."""
    spec = _spec(key)
    if tenant_id is not None:
        value = _lookup_row(db, key, tenant_id, "tenant")
        if value is not None:
            return _coerce(spec.type_, value), "db"
    value = _lookup_row(db, key, None, "platform")
    if value is not None:
        return _coerce(spec.type_, value), "db"
    if spec.env_fallback is not None and os.getenv(spec.env_fallback) is not None:
        return _coerce(spec.type_, os.getenv(spec.env_fallback)), "env"
    return spec.default, "default"


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def set_setting(key: str, value: Any, *, db,
                actor_user_id: Optional[int],
                tenant_id: Optional[int] = None) -> Any:
    """Validate + store a setting. SOFT-SUPERSEDES: the previous live row
    gets `deleted_at` stamped and a new row is inserted (history retained,
    ISO no-hard-delete); an append-only audit row records old/new/actor.

    Returns the coerced value. The caller owns the transaction — this
    flushes but does not commit."""
    spec = _spec(key)
    coerced = _coerce(spec.type_, value)
    if spec.validator is not None:
        spec.validator(coerced)

    scope = "tenant" if tenant_id is not None else "platform"

    # GY.C1 — profile locks (platform scope only): an active compliance
    # profile on a paid edition refuses a write that weakens a setting it
    # requires (409 + audit at the router). Tenant-scoped overrides do not
    # weaken the platform install's posture, so they are not locked.
    if tenant_id is None:
        # FX.162.p5 — compliance is paid-only (owner Q157): the CE carve
        # strips services/compliance_profiles.py, so no profile locks exist
        # there (CE fallback = the write is not locked).
        from utils.paid_import import optional_paid
        cprof = optional_paid("services.compliance_profiles")
        if cprof is not None:
            cprof.check_profile_lock(key, coerced, db=db,
                                     actor_user_id=actor_user_id)

    # GSEC R-159 — turning per-tenant HF cache isolation ON is refused until
    # every online worker has reported its one-time re-layout complete. A
    # separate small validator (not a settings-schema one): the gate is a
    # fleet-state check, not a value-shape check.
    if key == "models.hf_cache_tenant_isolation":
        # CARVE-U3: services/hf_cache_relayout.py is carve-denied (CE image has
        # no re-layout fleet to gate), so the bare import used to raise
        # ModuleNotFoundError. Absent -> the paid fleet gate cannot run; skip it.
        from utils.paid_import import optional_paid
        _hcrl = optional_paid("services.hf_cache_relayout")
        if _hcrl is not None:
            _hcrl.check_hf_cache_relayout_gate(coerced, db=db,
                                               actor_user_id=actor_user_id)

    # FX.103 p3a (owner decision Q89=A) — the runnable-window POLICY gate:
    # a deprecated node must stay runnable for at least 12 months, so a
    # window below that is refused unless the platform override flag
    # retirement.allow_below_minimum_window is already ON. Needs the
    # session the write commits into (the flag is read from the SAME
    # transaction, so a short window cannot be smuggled in by flipping the
    # flag in a separate request that fails afterwards), which a value-shape
    # validator cannot see — same shape as the fleet gates above.
    if key == "retirement.min_runnable_months":
        from services import node_retirement_scheduler as _fx103p3a
        _fx103p3a.enforce_window_minimum(coerced, db=db,
                                         actor_user_id=actor_user_id)

    # GY.C3b — phishing-resistant-only safety rail: enabling it while the
    # acting admin has no security key registered would lock EVERY human
    # sign-in out (service/machine accounts are exempt, so they can never
    # be the rescue path). Refused with 409, same as the fleet gates above.
    # The settings-write audit row (PlatformSettingAudit, old/new/actor)
    # records the change itself.
    if (key == "auth.phishing_resistant_only" and coerced is True
            and tenant_id is None):
        # CARVE-U3: services/webauthn_policy.py is carve-denied. Absent -> the
        # paid admin-has-a-security-key gate has no policy module to enforce;
        # skip the gate (the setting write below is unchanged).
        from utils.paid_import import optional_paid
        _webauthn_policy = optional_paid("services.webauthn_policy")
        if _webauthn_policy is None:
            logger.info("SKIPPED (not in this edition): "
                        "auth.phishing_resistant_only webauthn gate")
        else:
            from services._base import ConflictError
            try:
                webauthn_enabled_now = bool(
                    get_setting("auth.webauthn_enabled", db=db))
            except Exception:  # noqa: BLE001 — unreadable -> treat as disabled
                webauthn_enabled_now = False
            if not webauthn_enabled_now:
                raise ConflictError(
                    "Cannot enable phishing-resistant-only while WebAuthn is "
                    "disabled: enable auth.webauthn_enabled first so sign-in "
                    "with a security key is actually offered.")
            from keycloak_auth import keycloak_admin_client
            _webauthn_policy.ensure_admin_has_webauthn(
                db, actor_user_id, keycloak_admin_client)


    query = db.query(PlatformSetting).filter(
        PlatformSetting.key == key,
        PlatformSetting.scope == scope,
        PlatformSetting.deleted_at.is_(None),
    )
    if scope == "tenant":
        query = query.filter(PlatformSetting.tenant_id == tenant_id)
    else:
        query = query.filter(PlatformSetting.tenant_id.is_(None))
    existing = query.with_for_update().first()
    old_value = existing.value if existing is not None else None
    if existing is not None:
        existing.deleted_at = _utcnow()

    db.add(PlatformSetting(
        key=key,
        scope=scope,
        tenant_id=tenant_id,
        value=coerced,
        updated_by=actor_user_id,
    ))
    db.add(PlatformSettingAudit(
        key=key,
        scope=scope,
        tenant_id=tenant_id,
        old_value=old_value,
        new_value=coerced,
        actor_user_id=actor_user_id,
    ))
    db.flush()

    _purge_key(key, tenant_id)
    _publish_invalidate()
    if key == BANNER_SETTING_KEY and tenant_id is None:
        # AC-8: the realm is platform-wide, so only the platform-scope row
        # drives it. Push deferred to after_commit (see _schedule_banner_sync).
        _schedule_banner_sync(db, coerced)
    if tenant_id is None and key in WEBAUTHN_SETTING_KEYS:
        _schedule_webauthn_sync(db)
    if tenant_id is None and key in SESSION_LIMIT_SETTING_KEYS:
        # GY.C4b: the browser-flow session limiter follows the live settings.
        _schedule_session_limit_sync(db)
    if tenant_id is None and key in X509_SETTING_KEYS:
        # GY.C3c: the browser-flow X.509 execution follows the live settings.
        _schedule_x509_sync(db)
    if tenant_id is None and key in LOCKOUT_SETTING_KEYS:
        # GSEC R-096 (FX.30): the realm brute-force fields follow the
        # live lockout settings.
        _schedule_lockout_sync(db)
    if tenant_id is None and key in SESSION_TIER_SETTING_KEYS:
        # GSEC FX.54 part 2 (Q37=A/Q49=A): the realm-carried session tier +
        # remember-me fields follow the live settings.
        _schedule_session_tier_sync(db)
    if tenant_id is None and key in MFA_ENFORCEMENT_SETTING_KEYS:
        # GSEC FX.53: the enforcement scope changed — re-run the sweep
        # after the commit (immediate grace-stamping / action queueing
        # for the newly in-scope population).
        _schedule_mfa_enforcement_sweep(db)
    return coerced


def clear_setting(key: str, *, db,
                  actor_user_id: Optional[int],
                  tenant_id: Optional[int] = None) -> bool:
    """Soft-delete the live DB row for (key, scope, tenant) so the key
    reverts to env/default resolution. An append-only audit row records
    the removed value and the value the key now resolves to (env
    fallback if set, else the default). Returns True if a live row was
    removed; False means there was nothing to revert (no audit row
    written). SOFT-delete only — ISO no hard delete. Flushes; the caller
    owns the commit."""
    spec = _spec(key)
    scope = "tenant" if tenant_id is not None else "platform"

    query = db.query(PlatformSetting).filter(
        PlatformSetting.key == key,
        PlatformSetting.scope == scope,
        PlatformSetting.deleted_at.is_(None),
    )
    if scope == "tenant":
        query = query.filter(PlatformSetting.tenant_id == tenant_id)
    else:
        query = query.filter(PlatformSetting.tenant_id.is_(None))
    existing = query.with_for_update().first()
    if existing is None:
        return False

    # GY.C1 — profile locks: reverting a locked setting to env/default is
    # a weakening when the result is weaker than an active profile
    # requires (checked BEFORE any row is touched; platform scope only).
    if tenant_id is None:
        if spec.env_fallback is not None and os.getenv(spec.env_fallback) is not None:
            _revert_preview = _coerce(spec.type_, os.getenv(spec.env_fallback))
        else:
            _revert_preview = spec.default
        # FX.162.p5 — CE carve: no compliance profiles -> nothing to unlock.
        from utils.paid_import import optional_paid
        cprof = optional_paid("services.compliance_profiles")
        if cprof is not None:
            cprof.check_profile_lock_revert(key, _revert_preview, db=db,
                                            actor_user_id=actor_user_id)

    old_value = existing.value
    existing.deleted_at = _utcnow()

    if spec.env_fallback is not None and os.getenv(spec.env_fallback) is not None:
        reverted_to = _coerce(spec.type_, os.getenv(spec.env_fallback))
    else:
        reverted_to = spec.default
    db.add(PlatformSettingAudit(
        key=key,
        scope=scope,
        tenant_id=tenant_id,
        old_value=old_value,
        new_value=reverted_to,
        actor_user_id=actor_user_id,
    ))
    db.flush()

    _purge_key(key, tenant_id)
    _publish_invalidate()
    if key == BANNER_SETTING_KEY and tenant_id is None:
        # Reverting the platform row resolves to the default (empty) banner,
        # so the localization entry must go away — same deferred push.
        _schedule_banner_sync(db, str(reverted_to))
    if tenant_id is None and key in WEBAUTHN_SETTING_KEYS:
        # Reverting resolves to the registered defaults — the browser flow
        # must follow the same deferred apply.
        _schedule_webauthn_sync(db)
    if tenant_id is None and key in SESSION_LIMIT_SETTING_KEYS:
        # GY.C4b: reverting resolves to the defaults (0 = unlimited) — the
        # browser-flow limiter must follow the same deferred apply.
        _schedule_session_limit_sync(db)
    if tenant_id is None and key in X509_SETTING_KEYS:
        # GY.C3c: reverting resolves to the defaults (enabled=False) — the
        # browser-flow X.509 execution must follow the same deferred apply.
        _schedule_x509_sync(db)
    if tenant_id is None and key in LOCKOUT_SETTING_KEYS:
        # GSEC R-096: reverting resolves to the defaults (5 failures +
        # temporary backoff) — the realm must follow the same deferred
        # apply.
        _schedule_lockout_sync(db)
    if tenant_id is None and key in SESSION_TIER_SETTING_KEYS:
        # GSEC FX.54 part 2: reverting resolves to the registered defaults —
        # the realm session-tier + remember-me fields must follow the same
        # deferred apply.
        _schedule_session_tier_sync(db)
    if tenant_id is None and key in MFA_ENFORCEMENT_SETTING_KEYS:
        # GSEC FX.53: reverting resolves to the registered defaults —
        # the scope may have narrowed or the sweep may be off again, so
        # re-run it on the same deferred seam.
        _schedule_mfa_enforcement_sweep(db)
    return True


# ---------------------------------------------------------------------------
# AC-8 login banner -> Keycloak sync (auth.login_banner_text)
# ---------------------------------------------------------------------------
# The owner-approved AC-8 system-use notice lives in this registry; the
# Keycloak login theme renders it from the realm localization entry
# localization/en/modtechUseNotice (see CORE/startup/keycloak-init.sh and
# CORE/startup/keycloak-themes/modtech/login/template.ftl). When an admin
# edits the setting, push the new text to Keycloak so the login page picks
# it up without a Keycloak restart; empty text DELETEs the entry (msg()
# then falls back to the key itself, which the template treats as "no
# banner"). The push is DEFERRED to after the settings write commits — an
# external HTTP call inside the open DB transaction would pin a PgBouncer
# worker for the round-trip (see [[external-fetch-inside-db-transaction]]).
# A push failure NEVER fails the settings write: it is logged and recorded
# as a SECURITY_CONFIG_ERROR audit row (the audit/notification seam).
BANNER_SETTING_KEY = "auth.login_banner_text"
BANNER_LOCALIZATION_LANG = "en"
BANNER_LOCALIZATION_KEY = "modtechUseNotice"
# GY.C4b — declared here (module top) so the after-commit sync hook below
# can reference the keys; the register() calls are in the auth settings
# section further down.
SESSION_LIMIT_SETTING_KEY = "auth.session_max_concurrent"
SESSION_LIMIT_BEHAVIOUR_SETTING_KEY = "auth.session_limit_behaviour"
REAUTH_SETTING_KEY = "auth.privileged_reauth_max_age_s"
# GY.C3c — PIV/CAC client-certificate login (declared here so the
# after-commit sync hook below can reference the keys).
X509_ENABLED_SETTING_KEY = "auth.x509_enabled"
X509_USER_MAPPING_SETTING_KEY = "auth.x509_user_mapping"
X509_REVOCATION_SETTING_KEY = "auth.x509_revocation"
X509_OCSP_RESPONDER_URI_SETTING_KEY = "auth.x509_ocsp_responder_uri"
X509_SETTING_KEYS = (X509_ENABLED_SETTING_KEY,
                     X509_USER_MAPPING_SETTING_KEY,
                     X509_REVOCATION_SETTING_KEY,
                     X509_OCSP_RESPONDER_URI_SETTING_KEY)
# GSEC R-096 (FX.30) — login (brute-force) lockout policy (declared here
# so the after-commit sync hook below can reference the keys; the
# register() calls are in the auth settings section further down).
LOCKOUT_MAX_FAILURES_SETTING_KEY = "auth.lockout_max_failures"
LOCKOUT_WINDOW_MIN_SETTING_KEY = "auth.lockout_window_min"
LOCKOUT_DURATION_MIN_SETTING_KEY = "auth.lockout_duration_min"
LOCKOUT_PERMANENT_SETTING_KEY = "auth.lockout_permanent_until_admin"
LOCKOUT_SETTING_KEYS = (LOCKOUT_MAX_FAILURES_SETTING_KEY,
                        LOCKOUT_WINDOW_MIN_SETTING_KEY,
                        LOCKOUT_DURATION_MIN_SETTING_KEY,
                        LOCKOUT_PERMANENT_SETTING_KEY)
# GSEC FX.54 part 2 (owner Q37=A + Q49=A) — the realm-carried session tier (the
# most permissive tier the realm serves) + the remember-me trio. The ADMIN tier
# pair is NOT a realm field: it is enforced server-side in
# services/session_tier_enforcement.py, so changing it must not touch the realm.
SESSION_TIER_SETTING_KEYS = ("auth.sso_session_idle_seconds",
                             "auth.sso_session_max_lifetime_seconds",
                             "auth.remember_me_enabled",
                             "auth.remember_me_idle_seconds",
                             "auth.remember_me_max_lifetime_seconds")
_BANNER_PENDING_INFO_KEY = "psr_banner_pending_syncs"
_BANNER_LISTENER_INFO_KEY = "psr_banner_after_commit_listener"
# Whitelist (not a blacklist) so masked realm fields — client credentials,
# smtpServer.password, keystore — can never be re-submitted as "**********".
# attributes + the AC-7 brute-force block MUST be carried through or Keycloak
# 26.x re-applies its own defaults (the 2026-10-04 frontendUrl wipe).
_REALM_PUT_CARRY_THROUGH_FIELDS = (
    "attributes",
    "bruteForceProtected", "permanentLockout", "failureFactor",
    "waitIncrementSeconds", "maxFailureWaitSeconds", "maxDeltaTimeSeconds",
    "quickLoginCheckMilliSeconds", "minimumQuickLoginWaitSeconds",
    "loginTheme", "accountTheme", "adminTheme",
)


def _ensure_realm_localization(kc) -> None:
    """Enable realm internationalization (localization texts only render in
    the theme when it is on). PARTIAL realm PUT only — never the full
    RealmRepresentation (masked secrets such as smtpServer.password would be
    clobbered), and carry the existing locales through. Mirrors the
    localization-enable PUT in keycloak-init.sh.

    Keycloak 26.x re-applies its own defaults for realm fields a PUT omits,
    so the partial body must carry the live `attributes` map (frontendUrl!)
    and the AC-7 brute-force block through untouched — omitting them is what
    wiped frontendUrl during the 2026-10-04 login outage. Masked fields
    (credentials/smtp secrets) are NOT in the whitelist, so they are never
    re-submitted as "**********"."""
    realm_url = kc.admin_api_url
    resp = httpx.get(realm_url,
                     headers={"Authorization": f"Bearer {kc._get_token()}"},
                     timeout=30)
    resp.raise_for_status()
    existing = resp.json()
    locales = existing.get("supportedLocales") or []
    if "en" not in locales:
        locales = locales + ["en"]
    body = {"realm": existing.get("realm") or realm_url.rsplit("/", 1)[-1],
            "internationalizationEnabled": True,
            "supportedLocales": locales,
            "defaultLocale": existing.get("defaultLocale") or "en"}
    for field in _REALM_PUT_CARRY_THROUGH_FIELDS:
        if field in existing:
            body[field] = existing[field]
    put = httpx.put(realm_url,
                    headers={"Authorization": f"Bearer {kc._get_token()}"},
                    json=body, timeout=30)
    put.raise_for_status()


def _push_banner_to_keycloak(text: str) -> None:
    """Push the banner to Keycloak (PUT text, or DELETE when empty).
    Raises on Keycloak failure — the caller logs + audits, never fails the
    settings write."""
    from keycloak_auth import keycloak_admin_client
    kc = keycloak_admin_client
    token = kc._get_token()
    _ensure_realm_localization(kc)
    url = (f"{kc.admin_api_url}/localization/{BANNER_LOCALIZATION_LANG}"
           f"/{BANNER_LOCALIZATION_KEY}")
    if text:
        resp = httpx.put(url,
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "text/plain"},
                         content=text.encode("utf-8"), timeout=30)
    else:
        resp = httpx.delete(url,
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=30)
    resp.raise_for_status()


def _record_banner_sync_failure(db, text: str, exc: Exception) -> None:
    """Record a failed banner push (audit seam). Best-effort: an audit
    failure must not raise either.

    When called from the ``after_commit`` drain the session is already
    committed, so ``log_security_event``'s savepoint/commit would fail.
    Use a fresh session to guarantee the audit row lands.
    """
    try:
        from database import SessionLocal as _SL
        fresh = _SL()
        try:
            audit.log_security_event(
                fresh,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.login_banner_text.keycloak_sync_failed",
                          "banner_text": text,
                          "error": str(exc)[:500]},
            )
        finally:
            fresh.close()
    except Exception:  # noqa: BLE001
        logger.warning("platform_settings: banner-sync failure audit emit "
                       "failed (non-fatal)", exc_info=True)


def _on_banner_after_commit(session) -> None:
    """Outermost-commit drain: push every banner value set in the
    transaction that just committed. A rollback never fires this, so a
    discarded settings write never touches Keycloak."""
    pending = session.info.pop(_BANNER_PENDING_INFO_KEY, None) or []
    for text in pending:
        try:
            _push_banner_to_keycloak(text)
        except Exception as e:  # noqa: BLE001 — the setting is already saved
            logger.warning("platform_settings: AC-8 banner push to Keycloak "
                           "failed for %r (setting saved; keycloak-init.sh "
                           "will re-converge on next run): %s", text, e)
            _record_banner_sync_failure(session, text, e)


def _schedule_banner_sync(db, text: str) -> None:
    """Queue a banner push on the session; fires on the outermost commit
    (SQLAlchemy after_commit), dropped by a rollback."""
    from sqlalchemy import event
    if not db.info.get(_BANNER_LISTENER_INFO_KEY):
        event.listen(db, "after_commit", _on_banner_after_commit)
        db.info[_BANNER_LISTENER_INFO_KEY] = True
    db.info.setdefault(_BANNER_PENDING_INFO_KEY, []).append(text)


# GY.C3b — WebAuthn / phishing-resistant-only browser-flow sync. The two
# settings change the realm's browser flow; keycloak-init.sh converges the
# baseline on every run, but a settings change must take effect without a
# KC restart, so the apply is deferred to the outermost commit (same seam
# as the AC-8 banner) and a failure is logged + audited, never failing the
# settings write.
WEBAUTHN_SETTING_KEYS = ("auth.webauthn_enabled",
                         "auth.phishing_resistant_only")
_WEBAUTHN_PENDING_INFO_KEY = "psr_webauthn_pending_syncs"
_WEBAUTHN_LISTENER_INFO_KEY = "psr_webauthn_after_commit_listener"
# GSEC FX.53 (owner Q36=A) — MFA enforcement sweep. Only the master
# switch triggers an immediate after-commit sweep (the "cheap" on-change
# case): a scope change (platform all-users, the tenant
# auth.mfa_required_all_users setting, or a gov-profile apply) is picked
# up by the daily scheduler sweep within a day, so those keys are NOT in
# this set — keeping the settings-write path light and out of the way of
# the many compliance/MFA test flows.
MFA_ENFORCEMENT_ENABLED_SETTING_KEY = "auth.mfa_enforcement_enabled"
MFA_ENFORCEMENT_SETTING_KEYS = (MFA_ENFORCEMENT_ENABLED_SETTING_KEY,)
_MFA_ENFORCEMENT_PENDING_INFO_KEY = "psr_mfa_enforcement_pending_syncs"
_MFA_ENFORCEMENT_LISTENER_INFO_KEY = "psr_mfa_enforcement_after_commit_listener"


def _on_webauthn_after_commit(session) -> None:
    """Outermost-commit drain: apply the live WebAuthn browser-flow shape.
    A rollback never fires this, so a discarded settings write never
    touches Keycloak."""
    if not session.info.pop(_WEBAUTHN_PENDING_INFO_KEY, None):
        return
    # CARVE-U9a: services/webauthn_policy.py is carve-denied. Bind through
    # optional_paid so an absent module is an explicit CE skip (the paid
    # browser-flow sync it performs is not shipped in CE) — the old shape
    # raised ImportError inside the try and the handler logged a bogus
    # "Keycloak sync failed" SECURITY_CONFIG_ERROR, silently skipping the
    # gate (witness FAIL-OPEN).
    from utils.paid_import import optional_paid
    _webauthn_policy = optional_paid("services.webauthn_policy")
    if _webauthn_policy is None:
        logger.info("SKIPPED (not in this edition): "
                    "GY.C3b WebAuthn browser-flow sync")
        return
    try:
        from keycloak_auth import keycloak_admin_client
        result = _webauthn_policy.apply_browser_flow(
            session, keycloak_admin_client)
        logger.info("platform_settings: GY.C3b browser-flow apply: %s",
                    result)
    except Exception as e:  # noqa: BLE001 — the setting is already saved
        logger.warning("platform_settings: GY.C3b WebAuthn browser-flow "
                       "apply failed (setting saved; keycloak-init.sh will "
                       "re-converge on next run): %s", e)
        try:
            audit.log_security_event(
                session,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.webauthn.keycloak_sync_failed",
                          "error": str(e)[:500]})
        except Exception:  # noqa: BLE001
            logger.warning("platform_settings: webauthn sync-failure audit "
                           "emit failed (non-fatal)", exc_info=True)


def _schedule_webauthn_sync(db) -> None:
    """Queue a browser-flow apply on the session; fires on the outermost
    commit (SQLAlchemy after_commit), dropped by a rollback."""
    from sqlalchemy import event
    if not db.info.get(_WEBAUTHN_LISTENER_INFO_KEY):
        event.listen(db, "after_commit", _on_webauthn_after_commit)
        db.info[_WEBAUTHN_LISTENER_INFO_KEY] = True
    db.info[_WEBAUTHN_PENDING_INFO_KEY] = True


# GY.C4b — concurrent-session-cap browser-flow sync. Same seam as GY.C3b:
# the limit lives in the Keycloak browser flow, so a settings write pushes
# the new shape after the commit; a Keycloak failure is logged + audited,
# never fails the settings write (keycloak-init.sh re-converges on boot).
SESSION_LIMIT_SETTING_KEYS = (SESSION_LIMIT_SETTING_KEY,
                              SESSION_LIMIT_BEHAVIOUR_SETTING_KEY)
_SESSION_LIMIT_PENDING_INFO_KEY = "psr_session_limit_pending_syncs"
_SESSION_LIMIT_LISTENER_INFO_KEY = "psr_session_limit_after_commit_listener"


def _on_session_limit_after_commit(session) -> None:
    """Outermost-commit drain: apply the live session-limit browser-flow
    shape. A rollback never fires this, so a discarded settings write
    never touches Keycloak."""
    if not session.info.pop(_SESSION_LIMIT_PENDING_INFO_KEY, None):
        return
    try:
        from keycloak_auth import keycloak_admin_client
        from services import session_controls
        result = session_controls.converge_session_limiter(
            session, keycloak_admin_client)
        logger.info("platform_settings: GY.C4b session-limiter apply: %s",
                    result)
        if result.get("note", "").startswith("kc error") or \
                result.get("note", "").startswith("setting read"):
            audit.log_security_event(
                session,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.session_limit.keycloak_sync_failed",
                          "result": result})
    except Exception as e:  # noqa: BLE001 — the setting is already saved
        logger.warning("platform_settings: GY.C4b session-limiter apply "
                       "failed (setting saved; keycloak-init.sh will "
                       "re-converge on next run): %s", e)
        try:
            audit.log_security_event(
                session,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.session_limit.keycloak_sync_failed",
                          "error": str(e)[:500]})
        except Exception:  # noqa: BLE001
            logger.warning("platform_settings: session-limiter sync-failure "
                           "audit emit failed (non-fatal)", exc_info=True)


def _schedule_session_limit_sync(db) -> None:
    """Queue a session-limiter apply on the session; fires on the
    outermost commit (SQLAlchemy after_commit), dropped by a rollback."""
    from sqlalchemy import event
    if not db.info.get(_SESSION_LIMIT_LISTENER_INFO_KEY):
        event.listen(db, "after_commit", _on_session_limit_after_commit)
        db.info[_SESSION_LIMIT_LISTENER_INFO_KEY] = True
    db.info[_SESSION_LIMIT_PENDING_INFO_KEY] = True


# GY.C3c — PIV/CAC X.509 client-certificate browser-flow sync. Same seam as
# GY.C3b/GY.C4b: the execution lives in the Keycloak browser flow, so a
# settings write pushes the new shape after the commit; a Keycloak failure
# is logged + audited, never fails the settings write (keycloak-init.sh
# re-converges on boot).
_X509_PENDING_INFO_KEY = "psr_x509_pending_syncs"
_X509_LISTENER_INFO_KEY = "psr_x509_after_commit_listener"


def _on_x509_after_commit(session) -> None:
    """Outermost-commit drain: apply the live X.509 browser-flow shape. A
    rollback never fires this, so a discarded settings write never touches
    Keycloak."""
    if not session.info.pop(_X509_PENDING_INFO_KEY, None):
        return
    # CARVE-U9a: services/x509_policy.py is carve-denied. optional_paid binding
    # so absence is an explicit CE skip, not an ImportError swallowed by the
    # log-only handler (witness FAIL-OPEN).
    from utils.paid_import import optional_paid
    _x509_policy = optional_paid("services.x509_policy")
    if _x509_policy is None:
        logger.info("SKIPPED (not in this edition): "
                    "GY.C3c X.509 browser-flow sync")
        return
    try:
        from keycloak_auth import keycloak_admin_client
        result = _x509_policy.converge_x509(
            session, keycloak_admin_client)
        logger.info("platform_settings: GY.C3c X.509 apply: %s", result)
        if result.get("note", "").startswith("kc error") or \
                result.get("note", "").startswith("setting read"):
            audit.log_security_event(
                session,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.x509.keycloak_sync_failed",
                          "result": result})
    except Exception as e:  # noqa: BLE001 — the setting is already saved
        logger.warning("platform_settings: GY.C3c X.509 apply "
                       "failed (setting saved; keycloak-init.sh will "
                       "re-converge on next run): %s", e)
        try:
            audit.log_security_event(
                session,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.x509.keycloak_sync_failed",
                          "error": str(e)[:500]})
        except Exception:  # noqa: BLE001
            logger.warning("platform_settings: x509 sync-failure "
                           "audit emit failed (non-fatal)", exc_info=True)


def _schedule_x509_sync(db) -> None:
    """Queue an X.509 browser-flow apply on the session; fires on the
    outermost commit (SQLAlchemy after_commit), dropped by a rollback."""
    from sqlalchemy import event
    if not db.info.get(_X509_LISTENER_INFO_KEY):
        event.listen(db, "after_commit", _on_x509_after_commit)
        db.info[_X509_LISTENER_INFO_KEY] = True
    db.info[_X509_PENDING_INFO_KEY] = True


# GSEC FX.53 (owner Q36=A) — MFA enforcement sweep on scope changes.
# Same seam as GY.C3b/GY.C4b/GY.C3c: the in-scope population changed
# (master switch / platform all-users / profile list), so the sweep
# re-runs after the commit. The sweep's own audit rows are committed in
# the hook (they belong to the post-commit transaction, not the settings
# write's); a Keycloak failure is logged, never fails the settings write
# (the daily scheduler tick retries).
def _on_mfa_enforcement_after_commit(session) -> None:
    """Outermost-commit drain: re-run the MFA enforcement sweep. A
    rollback never fires this, so a discarded settings write never
    touches Keycloak."""
    if not session.info.pop(_MFA_ENFORCEMENT_PENDING_INFO_KEY, None):
        return
    # CARVE-U9a: services/mfa_enforcement.py is carve-denied. optional_paid
    # binding so absence is an explicit CE skip (the sweep is a paid feature),
    # not an ImportError swallowed by the log-only handler (witness FAIL-OPEN).
    from utils.paid_import import optional_paid
    _mfa_enforcement = optional_paid("services.mfa_enforcement")
    if _mfa_enforcement is None:
        logger.info("SKIPPED (not in this edition): "
                    "FX.53 MFA enforcement sweep")
        return
    try:
        from keycloak_auth import keycloak_admin_client
        counters = _mfa_enforcement.run_sweep(session, keycloak_admin_client)
        if counters.get("checked"):
            # run_sweep flushed audit rows into a fresh post-commit
            # transaction — commit them here so they are durable even
            # though the settings request is already done.
            session.commit()
        logger.info("platform_settings: FX.53 MFA enforcement sweep: %s",
                    counters)
    except Exception as e:  # noqa: BLE001 — the setting is already saved
        logger.warning("platform_settings: FX.53 MFA enforcement sweep "
                       "failed (setting saved; the daily sweep retries): "
                       "%s", e)


def _schedule_mfa_enforcement_sweep(db) -> None:
    """Queue an MFA enforcement sweep on the session; fires on the
    outermost commit (SQLAlchemy after_commit), dropped by a rollback."""
    from sqlalchemy import event
    if not db.info.get(_MFA_ENFORCEMENT_LISTENER_INFO_KEY):
        event.listen(db, "after_commit", _on_mfa_enforcement_after_commit)
        db.info[_MFA_ENFORCEMENT_LISTENER_INFO_KEY] = True
    db.info[_MFA_ENFORCEMENT_PENDING_INFO_KEY] = True


# GSEC R-096 (FX.30) — login lockout policy sync. Same seam as GY.C3c:
# the realm brute-force fields follow the live settings after the commit;
# a Keycloak failure is logged + audited, never fails the settings write
# (keycloak-init.sh re-converges on boot).
_LOCKOUT_PENDING_INFO_KEY = "psr_lockout_pending_syncs"
_LOCKOUT_LISTENER_INFO_KEY = "psr_lockout_after_commit_listener"


def _on_lockout_after_commit(session) -> None:
    """Outermost-commit drain: converge the realm brute-force fields. A
    rollback never fires this, so a discarded settings write never
    touches Keycloak."""
    if not session.info.pop(_LOCKOUT_PENDING_INFO_KEY, None):
        return
    # CARVE-U9a: services/lockout_policy.py is carve-denied. optional_paid
    # binding so absence is an explicit CE skip, not a silently-skipped sync
    # behind the log-only handler (witness FAIL-OPEN).
    from utils.paid_import import optional_paid
    _lockout_policy = optional_paid("services.lockout_policy")
    if _lockout_policy is None:
        logger.info("SKIPPED (not in this edition): "
                    "GSEC R-096 lockout realm-field sync")
        return
    try:
        from keycloak_auth import keycloak_admin_client
        result = _lockout_policy.converge_lockout(
            session, keycloak_admin_client)
        logger.info("platform_settings: GSEC R-096 lockout apply: %s",
                    result)
        if result.get("note", "").startswith("kc error") or \
                result.get("note", "").startswith("setting read"):
            audit.log_security_event(
                session,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.lockout.keycloak_sync_failed",
                          "result": result})
    except Exception as e:  # noqa: BLE001 — the setting is already saved
        logger.warning("platform_settings: GSEC R-096 lockout apply "
                       "failed (setting saved; keycloak-init.sh will "
                       "re-converge on next run): %s", e)
        try:
            audit.log_security_event(
                session,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.lockout.keycloak_sync_failed",
                          "error": str(e)[:500]})
        except Exception:  # noqa: BLE001
            logger.warning("platform_settings: lockout sync-failure "
                           "audit emit failed (non-fatal)", exc_info=True)


def _schedule_lockout_sync(db) -> None:
    """Queue a lockout realm-field apply on the session; fires on the
    outermost commit (SQLAlchemy after_commit), dropped by a rollback."""
    from sqlalchemy import event
    if not db.info.get(_LOCKOUT_LISTENER_INFO_KEY):
        event.listen(db, "after_commit", _on_lockout_after_commit)
        db.info[_LOCKOUT_LISTENER_INFO_KEY] = True
    db.info[_LOCKOUT_PENDING_INFO_KEY] = True


# GSEC FX.54 part 2 (owner Q37=A + Q49=A) — the realm-carried session tier +
# remember-me fields follow the live settings after the commit; a Keycloak
# failure is logged + audited, never fails the settings write (keycloak-init.sh
# re-converges on boot). The ADMIN tier pair is not a realm field — it is
# enforced server-side (services/session_tier_enforcement.py), so it is not in
# SESSION_TIER_SETTING_KEYS and must not schedule this sync.
_SESSION_TIER_PENDING_INFO_KEY = "psr_session_tier_pending_syncs"
_SESSION_TIER_LISTENER_INFO_KEY = "psr_session_tier_after_commit_listener"


def _on_session_tier_after_commit(session) -> None:
    """Outermost-commit drain: converge the realm session-tier + remember-me
    fields. A rollback never fires this, so a discarded settings write never
    touches Keycloak."""
    if not session.info.pop(_SESSION_TIER_PENDING_INFO_KEY, None):
        return
    try:
        from keycloak_auth import keycloak_admin_client
        from services import session_tier_enforcement as ste
        result = ste.converge_session_tier(session, keycloak_admin_client)
        logger.info("platform_settings: FX.54 session-tier apply: %s", result)
        if result.get("note", "").startswith("kc error") or \
                result.get("note", "").startswith("setting read"):
            audit.log_security_event(
                session,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.session_tier.keycloak_sync_failed",
                          "result": result})
    except Exception as e:  # noqa: BLE001 — the setting is already saved
        logger.warning("platform_settings: FX.54 session-tier apply failed "
                       "(setting saved; keycloak-init.sh will re-converge on "
                       "next run): %s", e)
        try:
            audit.log_security_event(
                session,
                models.AuditAction.SECURITY_CONFIG_ERROR,
                metadata={"event": "auth.session_tier.keycloak_sync_failed",
                          "error": str(e)[:500]})
        except Exception:  # noqa: BLE001
            logger.warning("platform_settings: session-tier sync-failure "
                           "audit emit failed (non-fatal)", exc_info=True)


def _schedule_session_tier_sync(db) -> None:
    """Queue a session-tier realm apply on the session; fires on the
    outermost commit (SQLAlchemy after_commit), dropped by a rollback."""
    from sqlalchemy import event
    if not db.info.get(_SESSION_TIER_LISTENER_INFO_KEY):
        event.listen(db, "after_commit", _on_session_tier_after_commit)
        db.info[_SESSION_TIER_LISTENER_INFO_KEY] = True
    db.info[_SESSION_TIER_PENDING_INFO_KEY] = True


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Admin listing
# ---------------------------------------------------------------------------

def list_settings(db) -> list:
    """One row per registered key (platform scope): value + source
    ('db' | 'env' | 'default') plus the registry metadata the admin page
    needs. `tenant_overrides` lists tenant_ids carrying a live override."""
    out = []
    _hidden = _ce_hidden_probe()
    for key in sorted(_REGISTRY):
        spec = _REGISTRY[key]
        if _hidden and (getattr(spec, "ce_hidden", False)
                        or key in PRO_FEATURE_KEYS_HIDDEN_ON_CE):
            # owner 75 (11-13): billing-only entries; owner 11-15 Q-B3 A:
            # PRO-FEATURE keys never listed on CE.
            continue
        value, source = resolve_source(key, db)
        overrides = [
            row[0] for row in db.query(PlatformSetting.tenant_id).filter(
                PlatformSetting.key == key,
                PlatformSetting.scope == "tenant",
                PlatformSetting.deleted_at.is_(None),
            ).all()
            if row[0] is not None
        ]
        out.append({
            "key": key,
            "type": spec.type_.__name__,
            "value": value,
            "source": source,
            "default": spec.default,
            "env_fallback": spec.env_fallback,
            "restart_required": spec.restart_required,
            "description": spec.description,
            "group": spec.group,
            "tenant_overrides": sorted(overrides),
        })
    return out


# ---------------------------------------------------------------------------
# CE "unlimited" defaults (CE has NO quantity caps
# and NO rate limits except notifications.max_recipients_per_call).
# FIX-CE-UNLIMITED-A/B/C legs REUSE this helper — keep the name and
# signature stable. Same fail-to-PAID shape as feature_throttle() and
# rate_limit_defaults() below (the repo's existing CE-default pattern):
# ``ce=None`` asks ``utils.paid_import.is_ce_edition()`` and ANY failure
# returns the PAID value, so a broken edition probe can never silently
# loosen a paid cap. Defined here (before the first register() call) so
# every module-level register() can use it.
# ---------------------------------------------------------------------------


def _ce_unlimited(paid_value, unlimited=0, ce: "bool | None" = None):
    """CE-only "unlimited" default: CE returns `unlimited` (the sentinel),
    paid / unknown / erroring edition probe returns `paid_value`."""
    if ce is None:
        try:
            from utils.paid_import import is_ce_edition
            ce = bool(is_ce_edition())
        except Exception:  # noqa: BLE001 — fail to the PAID value
            ce = False
    return unlimited if ce else paid_value


# ---------------------------------------------------------------------------
# Pre-registered settings (readers migrate onto these in GS.PSR.6)
# ---------------------------------------------------------------------------

register(
    "model_cache_enabled",
    bool,
    False,
    group="models",
    description="Enable the model cache layer.",
)
# GF.C4 — the manager-side model-cache service ADDRESS (the Dashboard
# "Model cache" panel's API proxy, `routers/model_cache_admin.py`, reads it
# here rather than from env). Same name the platform health check (GF.C0b.8)
# already uses as an env var, so existing deployments resolve identically
# through the env fallback until an operator overrides it in the UI.
# GSEC R-098 — model-catalog supply-chain pins. The catalog fetchers pin the
# committed SHA of each repo's main branch (services/model_catalog_stacks.py);
# this gate makes that STRICT: any fetcher still pointed at a moving ref
# (revision "main"/branch, or a pinned revision with no sha256 manifest) is
# refused at compose build instead of fetched. Default False so existing
# boxes keep pulling; an operator who turns it on must pin first.
register(
    "models.require_pinned_revision",
    bool,
    False,
    group="models",
    description="R-098: fail model-weights fetch compose builds whose repo "
                "is not pinned to a commit/tag (revision must not be "
                "'main') or that carry no sha256 manifest. Default False "
                "(recorded, not enforced).",
)
# GSEC R-098 — LocalAI backend `trust_remote_code` gate. The vendored
# LocalAI backends used to forward the client's per-request value straight
# into AutoTokenizer/vLLM (arbitrary code execution from a fetched model
# repo). The backends now force it off unless the LocalAI deployment itself
# opts in. NOTE: MOD does not deploy LocalAI (no LocalAI persistent stack,
# catalog entry, or compose-generation path exists — see
# the standalone LocalAI deployment docs (not part of Community Edition)), so this setting
# is ADVISORY: it records the admin's intent for audit/conformance reporting
# and is NOT bridged into any container env. The real gate is the
# LOCALAI_TRUST_REMOTE_CODE_ALLOWED env var on the standalone LocalAI
# deployment (CORE/DockerContainers/LocalAI/docker-compose.yaml).
register(
    "models.localai_trust_remote_code_allowed",
    bool,
    False,
    group="models",
    description="R-098 (advisory): records whether the standalone LocalAI "
                "deployment is expected to allow trust_remote_code=True "
                "(custom model code executes). Default False. MOD does not "
                "deploy LocalAI, so this setting does NOT set any container "
                "env — the enforced gate is LOCALAI_TRUST_REMOTE_CODE_ALLOWED "
                "on the LocalAI deployment itself (default false there); the "
                "vendored backends force it off and the client-supplied value "
                "can never enable it. Keep this in sync with that env for "
                "audit reporting.",
)
# GSEC R-159 — per-tenant HF weight-cache split. The flat shared HF cache let
# one tenant READ weights another tenant pulled (incl. gated/private repos
# fetched with that tenant's HF token). When on, NON-platform tenant stacks
# bind only their own `tenants/<id>/` subtree at the container HF root (their
# fetches — gated or not — land there) plus the shared `public/` subtree
# read-only; platform stacks (tenant 1) keep the flat root. Default OFF =
# legacy flat mount for every stack, so nothing breaks live until an operator
# flips it after the one-time re-layout (see
# tests/MANUAL_TEST_GSEC_FIXES.md, "R-159").
register(
    "models.hf_cache_tenant_isolation",
    bool,
    False,
    group="models",
    description="R-159: isolate the HuggingFace weight cache per tenant. "
                "On = non-platform tenant stacks mount tenants/<id>/ (their "
                "own fetches) + public/ read-only; platform stacks keep the "
                "flat cache. Default False (legacy flat cache for all).",
)
# FX.131 (Q118=A) — gov/DoD training posture: customer-trained models may only
# be trained on the customer's OWN on-prem workers, never on shared /
# MOD-hosted GPUs. The compliance profiles cmmc + fedramp_high_il5 pin this to
# True (PROFILE_REQUIRED_SETTINGS, req ("eq", True)) so applying the profile
# writes it and the profile lock rejects any later write that flips it back.
# Default False = no enforcement for CE / non-gov tenants (existing behaviour).
# Enforcing it at the training-scheduler dispatch choke point is the follow-up
# (FX.131 PART 2); the setting + profile lock land first so the gov posture is
# declared and locked even before the dispatch gate is wired.
register(
    "training.own_workers_only",
    bool,
    False,
    group="models",
    description="FX.131 (Q118=A): when True, training node dispatch is "
                "refused unless the claimed worker belongs to the caller's own "
                "on-prem fleet (gov/DoD posture). Pinned True by the cmmc and "
                "fedramp_high_il5 compliance profiles. Default False (no "
                "training-worker restriction for CE / non-gov tenants).",
)


register(
    "model_cache.endpoint",
    str,
    "http://model-cache:8080",
    env_fallback="MODEL_CACHE_URL",
    group="models",
    description="Base URL of the manager MOD model-cache service "
                "(internal address; default http://model-cache:8080, same "
                "name the GF.C0b.8 health check resolves). Used by the "
                "System → Model Cache panel proxy (stats / list / prefetch "
                "/ evict).",
)
# GF.C1b — "cache as a worker role": disk-size gate a worker must clear
# before it can be enabled as its site's model cache
# (`services/model_cache_registry.py:enable_site_cache`). Below this, the
# enable endpoint refuses with 409 rather than enabling a cache that will
# immediately thrash on disk pressure.
register(
    "model_cache.site_cache_min_free_gb",
    float,
    200.0,
    group="models",
    description="Minimum free disk (GB) a worker must report before it can "
                "be enabled as its site's model cache (GF.C1b).",
)


def _validate_non_negative_int(value: int) -> None:
    if value < 0:
        raise ValueError(f"{value!r} must be a non-negative integer")


# GF.C6.2 — the mod-model-cache service's LRU size caps. These were env-only
# (MODEL_CACHE_MAX_BYTES / MODEL_CACHE_TENANT_MAX_BYTES), which broke the
# settings rule; they are DB-backed settings now and the cache service reads
# them over its existing internal API channel
# (`routers/model_cache_internal.py` -> `GET /v1/internal/model-cache/settings`).
# The env vars stay as the offline-boot fallback the standalone container uses
# when the API is unreachable.
register(
    "model_cache.max_bytes",
    int,
    500 * 1024 * 1024 * 1024,
    env_fallback="MODEL_CACHE_MAX_BYTES",
    validator=_validate_non_negative_int,
    group="models",
    description="Shared model-cache bucket size cap in bytes (GF.C6.2), read "
                "by the mod-model-cache service over its internal API channel; "
                "MODEL_CACHE_MAX_BYTES is the offline-boot fallback.",
)
register(
    "model_cache.tenant_max_bytes",
    int,
    100 * 1024 * 1024 * 1024,
    env_fallback="MODEL_CACHE_TENANT_MAX_BYTES",
    validator=_validate_non_negative_int,
    group="models",
    description="Per-tenant model-cache bucket size cap in bytes (GF.C6.2), "
                "read by the mod-model-cache service over its internal API "
                "channel; MODEL_CACHE_TENANT_MAX_BYTES is the offline-boot "
                "fallback.",
)
# GF.C6.5 — tenant-attribution enforcement for the model cache. False (soft
# mode, default) lets un-attributed downloads resolve anonymously (logged,
# rate-limited) so legacy callers keep working; True makes the cache reject
# them with 401. Read by the cache service over the same internal /settings
# channel as the caps; MODEL_CACHE_REQUIRE_TENANT_TOKEN stays only as the
# offline-boot fallback on the standalone container.
register(
    "model_cache.require_tenant_token",
    bool,
    False,
    group="models",
    description="Require a verified per-tenant model-cache token on every "
                "download (GF.C6.5): off = soft mode, un-attributed requests "
                "resolve anonymously and are logged; on = such requests are "
                "rejected with 401.",
)
# GF.C0b.15b — the model-cache token carried by a RUNNING STACK as HF_TOKEN.
# Stacks re-mint only on deploy (there is no in-place env refresh — that is the
# GX.S3b worker op), so this TTL is what a stack that is up longer than a single
# default token lifetime is exposed to a 401 on gated fetches. Default 30 days
# (5x the 24h per-task default): long-running serving stacks outlive a day, and
# the cache re-fetches the tenant's REAL HF token per request, so a longer
# tenant-scoped token has no revocation cost here (real-token rotation still
# takes effect; only the tenant-attribution wrapper expires). The short-lived
# per-task path (routers/worker_tasks) deliberately does NOT use this TTL —
# task tokens stay at the 24h default.
def _validate_model_cache_stack_token_ttl(value: int) -> None:
    if value < 3600:
        raise ValueError(f"{value!r} must be at least 3600 seconds (1 hour)")
register(
    "model_cache.stack_token_ttl_seconds",
    int,
    30 * 24 * 60 * 60,
    validator=_validate_model_cache_stack_token_ttl,
    group="models",
    description="TTL of the model-cache token minted for a PERSISTENT STACK's "
                "containers as HF_TOKEN (GF.C0b.15b). Stacks re-mint only on "
                "deploy, so this must outlive expected stack uptime — default "
                "2592000 (30 days). Per-task model-cache tokens are "
                "unaffected (they keep the 24h default).",
)
register(
    "wizard_docs_driven_builds_enabled",
    bool,
    False,
    env_fallback="WIZARD_DOCS_DRIVEN_BUILDS_ENABLED",
    description="Docs-driven wizard builds (BuildPlan extraction, skipping "
                "the legacy Containerizer phase).",
)
register(
    "wizard_variant_fanout_universal",
    bool,
    False,
    env_fallback="WIZARD_VARIANT_FANOUT_UNIVERSAL",
    description="Fan wizard builds out across cpu/cuda/rocm/intel/npu "
                "variants.",
)
register(
    "node_trial_rate_limit_per_hour",
    int,
    60,
    env_fallback="NODE_TRIAL_RATE_LIMIT_PER_HOUR",
    group="marketplace",
    description="Max node try-before-you-buy attempts per tenant per hour "
                "(Redis rate limiter window 3600s).",
)
register(
    "telemetry_batching_enabled",
    bool,
    False,
    env_fallback="MOD_TELEMETRY_BATCHING",
    group="telemetry",
    description="Coalesce high-frequency telemetry writes into batched "
                "flushes (utils.queue_state.TelemetryBatcher). Off = one "
                "write per event.",
)
register(
    "orchestration_via_queue_enabled",
    bool,
    False,
    env_fallback="MOD_ORCHESTRATION_VIA_QUEUE",
    group="orchestration",
    description="Route Execute orchestration through the MOD task queue "
                "(orchestration consumer) instead of in-process "
                "BackgroundTasks.",
)
# GO.6e — durable trigger dispatch. dispatch_trigger_run commits a QUEUED
# run and hands off root-task dispatch to a daemon thread; a uvicorn worker
# recycle kills that thread, orphaning the run (0 NodeRuns, 0 tasks). The
# sweep (services/workflow_orchestrator.sweep_stranded_trigger_runs)
# re-drives such runs. Tunables live here (no env vars), default ON.
register(
    "orchestration.trigger_run_redispatch_enabled",
    bool,
    True,
    group="orchestration",
    description="GO.6e stranded trigger-run re-dispatch: when true, the "
                "scheduler sweep (and the boot sweep) re-run the root-task "
                "dispatch for QUEUED trigger-fired runs that have zero "
                "NodeRuns and zero ApiTaskQueue rows after the grace "
                "window. Off = no-op (runs are left to the zombie-run "
                "reconcile to finalize).",
)
register(
    "orchestration.trigger_run_redispatch_grace_minutes",
    int,
    3,
    group="orchestration",
    description="GO.6e re-dispatch grace: a QUEUED run with no NodeRuns and "
                "no tasks is only re-driven once its started_at is older "
                "than this many minutes (also the cooldown before a "
                "second re-drive of the same run).",
)
register(
    "orchestration.trigger_run_redispatch_max_attempts",
    int,
    2,
    group="orchestration",
    description="GO.6e re-dispatch attempt cap per run (tracked in the "
                "run's parameters marker). After the cap the run is left "
                "for the zombie-run reconcile to finalize as FAILED.",
)
register(
    "sse_legacy_token_query_enabled",
    bool,
    True,
    env_fallback="SSE_LEGACY_TOKEN_QUERY_ENABLED",
    group="streaming",
    description="Keep the deprecated `?access_token=<JWT>` SSE fallback "
                "alongside `?ticket=` for one release (mid-deploy compat).",
)
# GSEC R-066 — SSRF fence for tenant-supplied BYO-S3 endpoints. DB setting
# only (no env var): the guard lives in
# utils/storage_connection_test.py:check_endpoint_allowed().
register(
    "storage.byo_allow_private_endpoints",
    bool,
    False,
    group="storage",
    description="GSEC R-066: when False, BYO-S3 endpoints that "
                "resolve to private/loopback/link-local/CGNAT ranges are "
                "refused on Test and on save. Unset, CE behaves as True (single tenant, admin-created "
                "connections, LAN MinIO/NAS allowed) - set False explicitly to "
                "re-arm the guard there. The cloud-metadata range "
                "169.254.0.0/16 and metadata hostnames stay blocked either way.",
)
# GSEC R-113 — daily temp-prefix sweep of tenant object-storage buckets
# (uploads/temp/ + downloads/temp/): abort stale multipart uploads and
# remove unreferenced temp objects. Off by default (opt-in); when on, the
# daily scheduler job sweeps tenants not under legal hold.
def _validate_temp_sweep_max_age_hours(value: int) -> None:
    if int(value) < 24:
        raise ValueError(
            "storage.temp_sweep_max_age_hours must be >= 24 (floor keeps an "
            "active upload/download safe); got "
            f"{value!r}")


register(
    "storage.temp_sweep_enabled",
    bool,
    False,
    group="storage",
    description="GSEC R-113: run the daily temp-prefix sweep (abort "
                "abandoned multipart uploads + remove unreferenced objects "
                "under uploads/temp/ and downloads/temp/ in tenant buckets). "
                "Default off; no-op when disabled.",
)
register(
    "storage.temp_sweep_max_age_hours",
    int,
    72,
    group="storage",
    validator=_validate_temp_sweep_max_age_hours,
    description="GSEC R-113: minimum age (hours) before a temp object or "
                "multipart upload is eligible for the sweep. Floor is 24 "
                "so an active upload/download can never be swept; "
                "default 72.",
)
# GSEC R-088 / FX.32 — BYO-S3 house-copy grace window. A verified BYO
# migration leaves a full duplicate of every object in the HOUSE
# tenant-{id}-files bucket. Owner decision (Q13=A): after a VERIFIED
# migration (object count + per-object checksum/ETag/size match), the house
# copy is soft-deleted and the existing privacy-erasure sweep
# (services/scheduler.py::_run_privacy_erasure, gated by
# privacy.erasure_job_enabled) destroys those house bytes once this many days
# have passed. The tenant's own bucket is never touched — BYO stays
# report-only. Rows are never hard-deleted (ISO 27001); every step is
# audit-logged.
register(
    "storage.byo_house_copy_grace_days",
    int,
    30,
    group="storage",
    description="GSEC R-088 — days after a verified BYO-S3 migration before "
                "the privacy-erasure sweep purges the HOUSE-side duplicate "
                "bytes (the house tenant-{id}-files copy) for that tenant. "
                "The tenant's own bucket is never touched.",
)
register(
    "stack_resume_after_fence_enabled",
    bool,
    True,
    env_fallback=None,
    group="stacks",
    description="When a worker loses manager contact long enough to fence its "
                "stacks and then reconnects, the reconciler redeploys each "
                "fenced stack IN PLACE on the same worker (any ha_class) so it "
                "recovers automatically instead of staying down until a human "
                "redeploys. Off = report-only (GR.FEN.2).",
)
# BUG-1028h J3 — the fence lease was one manager-derived value pushed to every
# worker, so a whole-site outage fenced the entire fleet at the same second.
# The lease is now DB-tunable (env names kept so existing overrides still work)
# and per-worker jitter lets the fleet stagger. Jitter only ever EXTENDS a
# lease (see stack_observation.py), so it can never shorten the fence.
def _validate_stack_health_degrade_observations(value: int) -> None:
    if value < 1 or value > 100:
        raise ValueError(f"{value!r} must be between 1 and 100 observations")


def _validate_stack_lease_seconds(value: int) -> None:
    if value < 120:
        raise ValueError(f"{value!r} must be at least 120 seconds")


def _validate_stack_fence_margin_seconds(value: int) -> None:
    if value < 1 or value > 90:
        raise ValueError(f"{value!r} must be between 0 and 90 seconds")


def _validate_stack_fence_jitter_pct(value: int) -> None:
    if value < 0 or value > 25:
        raise ValueError(f"{value!r} must be between 0 and 25 percent")


register(
    "stack_observation_grace_s",
    int,
    180,
    env_fallback="STACK_OBSERVATION_GRACE_S",
    validator=_validate_stack_lease_seconds,
    group="stacks",
    description="How long a worker's 'what am I running' observation stays "
                "trustworthy before it means nothing (BUG-1028h J3). Must "
                "exceed the heartbeat interval; the fence lease and the "
                "manager gap period are derived from this one value.",
)
register(
    "stack_health_degrade_observations",
    int,
    3,
    env_fallback="STACK_HEALTH_DEGRADE_OBSERVATIONS",
    validator=_validate_stack_health_degrade_observations,
    group="stacks",
    description="How many consecutive worker observations must show a stack "
                "service restarting/unhealthy/exited-non-zero before its card "
                "says status_health=degraded and app routes to that service "
                "answer 503 (STACK-HEALTH-STATUS-1126). Recovery is immediate "
                "on the first healthy beat; only degradation needs the streak, "
                "so one flaky beat never condemns a stack. Default 3.",
)
register(
    "stack_fence_margin_s",
    int,
    60,
    env_fallback="STACK_FENCE_MARGIN_S",
    validator=_validate_stack_fence_margin_seconds,
    group="stacks",
    description="Headroom between a worker fencing (compose stop) and the "
                "manager re-placing the stack (BUG-1028h J3). Too small and "
                "the re-placement overlaps the old copy still shutting down "
                "— the split-brain window fencing exists to close.",
)
register(
    "stack_fence_jitter_pct",
    int,
    0,
    env_fallback=None,
    validator=_validate_stack_fence_jitter_pct,
    group="stacks",
    description="Per-worker spread of the pushed fence lease as a percentage "
                "(BUG-1028h J3). A whole-site outage makes every lease expire "
                "at the same second and fences the fleet in lockstep; jitter "
                "staggeres it. Jitter only ever EXTENDS a lease, never "
                "shortens it, and the API clamps lease+jitter to "
                "grace - margin. Default 0 = behaviour unchanged.",
)
register(
    "billing_basis",
    str,
    "flat",
    env_fallback="BILLING_BASIS",
    validator=lambda v: _validate_billing_basis(v),
    group="billing",
    description="How run costing is applied: flat (record the estimate), "
                "shadow (compute actuals, audit only), run_costing "
                "(settle actuals against the estimate).",
)
register(
    "relay_metering_enabled",
    bool,
    False,
    env_fallback="RELAY_METERING_ENABLED",
    group="billing",
    description="Accumulate per-tenant MOD-DERP relay bytes into "
                "tenant_relay_usage_daily (scheduler tick, leader only).",
)
# GSEC FX.35 (Q17=C) — per-plan failed-payment grace window, in days.
# DB setting only (no env var): utils/grace_period.py resolves the window by
# the subscription's billing cadence. monthly-billed plans use the monthly
# key; annual plans use the annual key.
register(
    "billing.payment_grace_days_monthly",
    int,
    7,
    group="billing",
    ce_hidden=True,
    description="Grace window (days) after a failed payment on a hosted (SaaS) "
                "monthly-billed plan before the account is downgraded to the "
                "free tier.",
)
register(
    "billing.payment_grace_days_annual",
    int,
    14,
    group="billing",
    ce_hidden=True,
    description="Grace window (days) after a failed payment on a hosted (SaaS) annual "
                "plan before the account is downgraded to the free "
                "tier.",
)
# GSEC FX.41 (owner Q23=A) — plan-downgrade overage -> read-only. When a
# tenant's plan is downgraded and usage exceeds the new limits, new creates
# of the limited resource are blocked (403), excess seats are suspended
# (status change, never deleted), and the tenant is notified. Set false to
# disable the whole behaviour (limits then behave as pre-FX.41: warn/queue
# but never hard-block a NEW create).
register(
    "billing.overage_restrict_enabled",
    bool,
    True,
    group="billing",
    ce_hidden=True,
    description="After a hosted (SaaS) plan downgrade, block new creates of resources "
                "whose usage exceeds the new plan limit (storage / seats / "
                "concurrent workflows), suspend excess seats, and notify. "
                "Existing files and runs are never blocked or removed.",
)
register(
    "fixture_shared_bucket_enabled",
    bool,
    False,
    env_fallback="MOD_FIXTURE_SHARED_BUCKET",
    group="calibration",
    description="PFM.19 cutover: shared platform calibration-fixture "
                "bucket — gates the calibration readiness check, the "
                "create-tenant provisioning hook and the daily drift "
                "audit. Read as a boolean: 1/true/yes/on = on; 0/false/no/off "
                "or empty = off. A value the settings API cannot parse is "
                "rejected on write, and the gate treats an unparseable stored "
                "value as off.",
)
register(
    "noah.require_pinnable_enabled",
    bool,
    True,
    env_fallback="NOAH_REQUIRE_PINNABLE",
    group="noah",
    description="Whether pin-prone dispatch (stack ops, profiling, volume "
                "ops) requires the `pinnable` capability at claim time. "
                "Default True: every "
                "worker pinnable by default, with only NOAH idle-harvest "
                "hosts (and any worker with an explicit `not-pinnable` "
                "hardware tag) excluded, so the old fail-closed 'off "
                "until boxes are hand-tagged' posture no longer applies. "
                "Legacy env NOAH_REQUIRE_PINNABLE still wins when no "
                "platform row is set (migration bridge).",
)


_BILLING_BASIS_VALUES = ("flat", "shadow", "run_costing")


def _validate_billing_basis(value: str) -> None:
    if value not in _BILLING_BASIS_VALUES:
        raise ValueError(
            f"invalid billing basis {value!r}; expected one of "
            f"{sorted(_BILLING_BASIS_VALUES)}"
        )

# ---------------------------------------------------------------------------
# GS.PSR.7 — worker tunables delivered from the API instead of a per-box env
# recreate. No env_fallback: the API process's own env is not the Go worker's
# env, so there is nothing meaningful to fall back to here — the worker keeps
# consulting ITS OWN local env/default whenever the platform has no value set
# (see routers/worker_management.py:_resolve_worker_tunables, which only
# includes a key in the heartbeat `tunables` map when source == "db").
# ---------------------------------------------------------------------------


def _validate_egress_proxy_mode(value: str) -> None:
    if value not in ("enforce", "off"):
        raise ValueError(
            f"worker.egress_proxy_mode must be 'enforce' or 'off', got {value!r}"
        )


register(
    "worker.egress_proxy_mode",
    str,
    "enforce",
    validator=_validate_egress_proxy_mode,
    restart_required=False,
    group="worker",
    description="Egress proxy mode the Go worker applies to a task's "
                "FromAllowlist network verdict: 'enforce' rewrites it into "
                "the proxy bridge (default), 'off' disables the rewrite. "
                "Mirrors the worker's local MOD_EGRESS_PROXY env var, which "
                "still applies when no platform value is set. Delivered via "
                "the heartbeat response's `tunables` map and takes effect "
                "within two heartbeats — no worker restart.",
)


def _validate_docker_proxy_mode(value: str) -> None:
    if value not in ("off", "report", "enforce"):
        raise ValueError(
            f"worker.docker_proxy_mode must be one of 'off', 'report', "
            f"'enforce', got {value!r}"
        )


register(
    "worker.docker_proxy_mode",
    str,
    "off",
    validator=_validate_docker_proxy_mode,
    restart_required=False,
    group="worker",
    description="Q20 Docker socket proxy mode. 'off' (default) leaves "
                "containers holding the raw /var/run/docker.sock "
                "exactly as before. 'report' routes their docker calls "
                "through the worker's policy-filtering proxy, which "
                "forwards everything but logs what it would deny. "
                "'enforce' answers denied calls with a 403 (privileged "
                "containers, /var/run/docker.sock or /etc//proc//sys "
                "binds, host pid/net/ipc/uts/userns/cgroupns modes, "
                "capabilities beyond the allowlist, non-nvidia "
                "runtime/device requests; GPU passthrough stays "
                "allowed). Delivered via the heartbeat `tunables` map; "
                "takes effect within two heartbeats, no worker restart.",
)
register(
    "worker.prefer_local_images",
    bool,
    False,
    restart_required=False,
    group="worker",
    description="Skip re-pulling a task's image when it is already cached "
                "locally (offline/local-patch override for stage hosts "
                "running patched images). Paid editions: the same switch also covers stage hosts whose patched images are not yet published to Harbor. "
                "Mirrors the worker's local MOD_WORKER_PREFER_LOCAL_IMAGES "
                "env var, which still applies when no platform value is "
                "set. Delivered via the heartbeat response's `tunables` "
                "map and takes effect within two heartbeats — no worker "
                "restart.",
)
register(
    "worker.harbor_proxy_cache",
    bool,
    False,
    restart_required=False,
    group="worker",
    description="Pull public images via Harbor proxy-cache projects "
                "(GF.C3). When on, the Go worker rewrites a public image "
                "ref (docker.io/nvcr.io/ghcr.io) to Harbor's matching "
                "proxy-* project before pulling, then tags the pulled "
                "image back to the original ref so compose/containers "
                "referencing the original name still find it; any proxy "
                "error falls back to pulling the original ref. Delivered "
                "via the heartbeat response's `tunables` map and takes "
                "effect within two heartbeats — no worker restart.",
)
# Megaplan 25.5 — worker self-cleanup policy (worker v1.0.76,
# internal/executor/cleanup_bridge.go). Defaults match the worker's built-in
# cleanup.DefaultConfig() exactly; the reclaim gate defaults to OFF so an
# untouched fleet stays report-only. Delivered via the heartbeat response's
# `tunables` map (GS.PSR.7) — no worker restart.


def _validate_cleanup_watermark(value: int) -> None:
    if not 0 <= value <= 100:
        raise ValueError(
            f"worker.cleanup_image_*_water_pct must be 0-100, got {value!r}"
        )


def _validate_cleanup_model_cache_max_gb(value: int) -> None:
    if not 0 <= value <= 100000:
        raise ValueError(
            f"worker.cleanup_model_cache_max_gb must be 0-100000 "
            f"(0 disables model-cache eviction), got {value!r}"
        )


def _validate_cleanup_grace_hours(value: int) -> None:
    if not 1 <= value <= 168:
        raise ValueError(
            f"worker.cleanup_grace_hours must be 1-168, got {value!r}"
        )


register(
    "worker.cleanup_reclaim",
    bool,
    False,
    restart_required=False,
    group="worker",
    description="Enable the worker's self-cleanup RECLAIM gate (megaplan "
                "25.5). Report-only unless enabled; deletes only mod.* "
                "orphans (task containers/volumes/networks, task scratch "
                "dirs, idle model-cache weights), never pinned or "
                "in-use images. Off keeps the built-in report-only "
                "behaviour — every 30-minute pass computes what is "
                "reclaimable and reports it on the heartbeat `cleanup` "
                "key without deleting anything. Delivered via the "
                "heartbeat response's `tunables` map and takes effect "
                "within two heartbeats — no worker restart.",
)
register(
    "worker.cleanup_image_high_water_pct",
    int,
    85,
    validator=_validate_cleanup_watermark,
    restart_required=False,
    group="worker",
    description="Docker data-root usage percentage at or above which the "
                "worker's self-cleanup image GC fires (megaplan 25.5). "
                "Matches the worker's built-in default (85). Delivered "
                "via the heartbeat response's `tunables` map — no worker "
                "restart.",
)
register(
    "worker.cleanup_image_low_water_pct",
    int,
    75,
    validator=_validate_cleanup_watermark,
    restart_required=False,
    group="worker",
    description="Docker data-root usage percentage the worker's "
                "self-cleanup image GC reclaims down to (megaplan 25.5). "
                "Matches the worker's built-in default (75). The worker "
                "clamps this to high water if set higher — no worker "
                "restart.",
)
register(
    "worker.cleanup_model_cache_max_gb",
    int,
    # FIX-CE-UNLIMITED-WORKER-CLEANUP-MODEL-CACHE-MAX-GB: CE default is the
    # sentinel 0 = no model-cache eviction (the worker reads a 0 cap as
    # "eviction off", never "delete everything": internal/cleanup/planner.go
    # PlanModelDirs). Paid / probe error stays 500.
    _ce_unlimited(500),
    validator=_validate_cleanup_model_cache_max_gb,
    restart_required=False,
    group="worker",
    description="Cap (GiB) on the worker's on-box model cache; LRU "
                "eviction of unmounted, un-pinned weights runs only "
                "while the cache total is above the cap (megaplan 25.5). "
                "0 (default) disables "
                "model-cache eviction entirely. Delivered via the "
                "heartbeat response's `tunables` map — no worker restart.",
)
register(
    "worker.cleanup_grace_hours",
    int,
    24,
    validator=_validate_cleanup_grace_hours,
    restart_required=False,
    group="worker",
    description="Nothing created (containers/volumes/networks/task "
                "dirs) or last used (images/models) within this many "
                "hours is ever touched by the worker's self-cleanup "
                "(megaplan 25.5). Matches the worker's built-in default "
                "(24). Delivered via the heartbeat response's "
                "`tunables` map — no worker restart.",
)
# GF.C0b.9 — model-cache GC interlock. Registered here so the key is a
# first-class, admin-visible platform setting; the DELIVERED value is
# derived, not operator-entered (see worker_management.py:
# _resolve_worker_tunables), which is why it is deliberately absent from
# the _WORKER_TUNABLE_KEYS resolve_source loop.
register(
    "worker.model_mirror_endpoint",
    str,
    "",
    restart_required=False,
    group="worker",
    description="HF endpoint of the platform model mirror "
                "(models.<BASE_DOMAIN>) delivered to the Go worker so its "
                "on-box model-cache GC can evict mirror-sourced weights "
                "before WAN-sourced ones (GF.C0b.9). The delivered value is "
                "DERIVED by the API from managed_ai_stacks."
                "model_cache_endpoint() — exactly while model_cache_enabled "
                "is on — not operator-entered. Empty means the mirror is "
                "off for this deployment and the worker's GC keeps its "
                "plain-LRU ordering.",
)
# GSEC R-141 (SEC-2c-005) — fail-closed posture for the two internal-token
# gates: the Caddy on-demand-TLS `ask` seam (the ingress ask token, read from
# Vault at `infrastructure/ingress` by routers/persistent_stacks/_shared.py
# _ingress_ask_token(), see _ingress_token_ok) and the mesh-reachability seam
# (MOD_MESH_REACHABILITY_TOKEN env, _mesh_reach_token_ok). When the seam's token
# is PROVISIONED the gate always enforces it (wrong/missing token ⇒ 403)
# regardless of this setting. When the token is UNSET this setting decides the
# seam's behaviour:
# (wrong/missing token ⇒ 403) regardless of this setting. When the token env is
# UNSET this setting decides the seam's behaviour:
#   True  — FAIL CLOSED: refuse the request (the safe target posture).
#   False — legacy pass-through: allow, and log it loudly (throttled) so the
#           provisioning gap stays visible.
# Default is False — LIVE-SAFETY: the live deployment still lacks
# MOD_MESH_REACHABILITY_TOKEN, and a default-True flip would break the D6
# nginx reachability resolver on the next API restart. vault-init.sh now
# provisions both tokens on first boot (the ingress ask token lives in the
# Vault `infrastructure/ingress` slot, which the API reads directly at call
# time — CONSIGN §0.1; the mesh token still renders into env), so once both
# are provisioned an operator seeds True to complete R-141.
register(
    "security.stack_tokens_fail_closed",
    bool,
    False,
    group="security",
    description="GSEC R-141 — when a seam's token is UNSET (the ingress-ask "
                "token at Vault infrastructure/ingress, or the "
                "mesh-reachability token env), refuse the request instead of "
                "passing it through. Default False (live still lacks the mesh "
                "token); seed True once the ingress ask token and "
                "MOD_MESH_REACHABILITY_TOKEN are provisioned (vault-init "
                "seeds both on first boot).",
)
# GSEC FX.1 — per-tenant cap on PENDING+ASSIGNED tasks a NON-admin caller
# may have queued through POST /workers/tasks/enqueue (worker_tasks.py).
# The endpoint previously accepted any tenant_id + free-form payload from
# any authenticated caller (R-003/R-052); the HTTP wrapper now scopes
# non-admin enqueues to the caller's own tenant and refuses a new task
# once the caller's tenant is already over this cap. Admin / service
# principals are unaffected. DB-backed tunable (never env), default 200.
# FIX-CE-UNLIMITED-C (CE has NO quantity caps): the
# CE default is the sentinel 0 = unlimited — the SAME sentinel leg A uses for
# the count caps. This key has NO validator, so 0 passes validation unchanged;
# the consumer (routers/worker_tasks.py enqueue cap) carries the matching
# `cap > 0` guard, a no-op for every finite positive limit. Paid keeps 200; an
# explicit platform/tenant row wins on BOTH editions.
register(
    "worker.tenant_enqueue_max_pending",
    int,
    _ce_unlimited(200),
    group="worker",
    description="GSEC FX.1 — max PENDING+ASSIGNED api_task_queue rows a "
                "single tenant may hold that were enqueued through the "
                "public POST /workers/tasks/enqueue before non-admin "
                "callers get 429. 0 = unlimited on CE "
                "(no quantity cap on CE). Platform "
                "admins / service principals are exempt. Raise for tenants "
                "with large fan-out runs.",
)
# GSEC R-031 — credential gate for the embedded (on-manager) worker's
# task-plane auth. The Go embedded worker reads its bootstrap token from
# Vault KV v2 `infrastructure/embedded-worker` (GetInfraSecret) and the API
# startup seeds the hash onto an APPROVED WorkerEnrollment row
# (main.py:_startup_seed_embedded_worker_enrollment), so it authenticates
# exactly like a remote worker. While this is True (the default, and the
# posture every NEW install gets) a worker with no enrollment row is refused
# on the strength of its name only — X-Bootstrap-Token is mandatory. For the
# live install (which was deployed before the embedded token existed) the
# lead seeds a False platform row so the embedded worker keeps working
# name-only until its binary is verified carrying the Vault token, then
# flips it to True. While False, every name-only acceptance is logged +
# written to the audit trail (reason
# gsec_r031_embedded_name_only_acceptance) so the operator can see who
# relied on the legacy path before flipping.
register(
    "worker.embedded_requires_secret",
    bool,
    True,
    group="worker",
    description="GSEC R-031 — require the embedded worker's bootstrap token "
                "(enrollment auth) on the /workers task plane. Default True: "
                "name-only auth is refused. Seed False on the live install "
                "only while the embedded worker binary is still verified to "
                "be carrying the Vault token; every name-only acceptance "
                "while off is audit-logged.",
)
register(
    "worker.token_verify_cache_ttl_s",
    int,
    120,
    group="worker",
    description="Seconds a SUCCESSFUL worker bootstrap-token bcrypt check is "
                "remembered in-process (keyed to the exact stored hash, so a "
                "rotated/revoked token misses at once; wrong tokens are never "
                "cached). Cuts ~0.24 s of CPU from every worker task-plane "
                "call. 0 = verify with bcrypt on every request (takes effect "
                "within about 5 s and also drops entries already cached; a "
                "lower value drops older entries too). Values above 3600 are "
                "clamped to 3600.",
)
# GF.C3 — platform-Harbor proxy-cache bootstrap kill switch. Off only
# stops the startup step from calling out to platform Harbor; it never
# deletes an already-ensured registry/project.
register(
    "harbor_proxy_cache_enabled",
    bool,
    True,
    group="harbor",
    description="Idempotently ensure Harbor registry endpoints + "
                "proxy-cache projects (proxy-dockerhub/proxy-nvcr/"
                "proxy-ghcr) exist on platform Harbor at API startup, so "
                "node/NIM image pulls can be rewritten to go through "
                "Harbor's cache (services.harbor_proxy_cache). Off skips "
                "the startup ensure step entirely — set False on a "
                "deployment with no platform Harbor reachable.",
)
# GSEC FX.6 (R-071) — image signing / provenance. cosign (sigstore) keyed
# verification of the node images the API hands to a worker, at the single
# dispatch seam in routers/worker_tasks.py:enqueue_task_internal. DB-backed
# only (never env). Rollout is report-only first: default "off" is a no-op
# that changes nothing; "report" logs verify results; "enforce" is the
# fail-closed phase. See docs/security-audit/design/R-071-image-signing.md.


def _validate_image_signature_mode(value: str) -> None:
    if value not in ("off", "report", "enforce"):
        raise ValueError(
            f"supply_chain.image_signature_mode must be one of "
            f"'off'|'report'|'enforce', got {value!r}"
        )


register(
    "supply_chain.image_signature_mode",
    str,
    "off",
    validator=_validate_image_signature_mode,
    restart_required=False,
    group="supply_chain",
    description="GSEC FX.6 (R-071) — image signing/provenance mode applied "
                "at the node-task dispatch seam. 'off' (default) disables "
                "verification entirely and is a no-op; 'report' attempts a "
                "cosign verify and logs the result without blocking "
                "dispatch; 'enforce' is fail-closed: the live seam refuses "
                "the enqueue with HTTP 422 when verification fails, cosign "
                "tooling is missing, or the verifier itself crashes.",
)
register(
    "supply_chain.cosign_public_key_ref",
    str,
    "",
    restart_required=False,
    group="supply_chain",
    description="GSEC FX.6 (R-071) — reference to the cosign public key used "
                "for image verification. Empty means verification cannot "
                "run (report/enforce report 'unavailable'). Key material is "
                "custodied in Vault and resolved to a local file before it "
                "is passed to `cosign verify --key <path>` — the raw key is "
                "never stored in env or git. See "
                "docs/security-audit/design/R-071-image-signing.md.",
)
# GX.R4 — ephemeral run inputs. Owner decision OQ-5(a): TTL 24h -> 7d grace
# -> bytes purged in ALL modes (even ISO); the record row is kept forever.
# These are the DB-backed tunables (services.ephemeral_run_inputs reads them);
# never env.
register(
    "ephemeral_inputs.ttl_hours",
    int,
    24,
    group="ephemeral",
    description="Hours before an ephemeral run input expires (status flips to "
                "'expired'; runs referencing it get 410 Gone).",
)
register(
    "ephemeral_inputs.purge_grace_days",
    int,
    7,
    group="ephemeral",
    description="Days of grace after expiry before the storage object BYTES "
                "are purged (all modes). The record "
                "row is never deleted.",
)
# 30.6.12 c12a — batch ephemeral-input upload caps (POST /v1/runs/ephemeral-
# inputs with N `file` parts). DB-backed, never env; enforced BEFORE any DB
# write or storage put (services.ephemeral_run_inputs.batch_max_*). The
# per-file cap is NOT a new key — it stays the upload_size_cap "cog"
# category (uploads.max_bytes_cog above).
def _validate_batch_max_files(value: int) -> None:
    if not (1 <= int(value) <= 64):
        raise ValueError(
            f"{value!r} is outside the 1..64 batch file-count range — "
            "refusing a cap that would reject every batch or allow a huge one")


register(
    "ephemeral_inputs.batch_max_files",
    int,
    16,
    group="ephemeral",
    validator=_validate_batch_max_files,
    description="Maximum number of `file` parts one batch ephemeral-input "
                "upload may carry (over-cap => 413 batch_too_many_files, "
                "nothing written).",
)
register(
    "ephemeral_inputs.batch_max_total_bytes",
    int,
    250 * 1024 * 1024,
    group="ephemeral",
    description="Maximum SUMMED bytes one batch ephemeral-input upload may "
                "carry (over-cap => 413 batch_too_large, nothing written). "
                "Per-part bytes stay capped by uploads.max_bytes_cog.",
)
# GX.S4 — general email send API (routers/email_send_api.py). Limits for
# POST /v1/email/send, enforced in services/email_send_api.limit_bundle
# (tenant row wins over these defaults). Never env.
register(
    "email.max_recipients",
    int,
    _ce_unlimited(25),
    group="email",
    description="Max total recipients (to+cc+bcc) per message on "
                "POST /v1/email/send (422 above this). "
                "CE default is 0 (unlimited); paid default is 25.",
)
register(
    "email.max_total_size_bytes",
    int,
    _ce_unlimited(10 * 1024 * 1024),
    group="email",
    description="Max total bytes (text + html + attachments) per message on "
                "POST /v1/email/send (413 above this). "
                "CE default is 0 (unlimited); paid default is 10MB.",
)
register(
    "email.max_attachments",
    int,
    _ce_unlimited(5),
    group="email",
    description="Max attachment count per message on POST /v1/email/send "
                "(422 above this). "
                "CE default is 0 (unlimited); paid default is 5.",
)
register(
    "email.rate_limit_per_minute",
    int,
    # FIX-CE-UNLIMITED-B1: CE = 0 (unlimited; consumers treat <=0 as no limit), paid = 30.
    _ce_unlimited(30),
    group="email",
    description="Per-tenant email send rate (requests/minute, Redis sliding "
                "window) for POST /v1/email/send (429 above this).",
)
register(
    "email.rate_limit_per_hour",
    int,
    # FIX-CE-UNLIMITED-B1: CE = 0 (unlimited; consumers treat <=0 as no limit), paid = 300.
    _ce_unlimited(300),
    group="email",
    description="Per-tenant email send rate (requests/hour, Redis sliding "
                "window) for POST /v1/email/send (429 above this).",
)
# GX.S5 — tenant LLM endpoint (routers/tenant_llm_api.py). Limits for
# POST /v1/llm/chat/completions, enforced in
# services/tenant_llm_api._precheck (tenant row wins over these
# defaults). Never env. The monthly token quota is only ENFORCED for
# Paid tenants (usage_metering capability); CE gets size + rate limits.
register(
    "llm.max_request_bytes",
    int,
    2 * 1024 * 1024,
    group="llm",
    description="Max request body bytes on POST /v1/llm/chat/completions "
                "(413 above this). Default 2MB.",
)
register(
    "llm.rate_limit_per_minute",
    int,
    # FIX-CE-UNLIMITED-B1: CE = 0 (unlimited; consumers treat <=0 as no limit), paid = 60.
    _ce_unlimited(60),
    group="llm",
    description="Per-tenant LLM request rate (requests/minute, Redis "
                "sliding window) for POST /v1/llm/chat/completions "
                "(429 above this).",
)
# FIX-CE-UNLIMITED-E (CE has NO quantity caps). The two
# per-tenant LLM window volumes that services/llm_rate_limit.py resolves when
# nothing is configured. Registered here so the DEFAULT is edition-aware
# through the shared _ce_unlimited helper: 0 = UNLIMITED on CE, and byte-for-
# byte the old number on Paid (1200 billable / 600 assistant). The limiter
# already treats 0 as unlimited (it maps a plan's 0 to UNLIMITED_CALLS = 1e9),
# so no new sentinel is introduced and no new env var is added — the existing
# LLM_RATE_LIMIT_* env vars remain the deployment override inside
# llm_rate_limit.py, and the platform_llm_rate_limits / tenant_llm_rate_limits
# DB rows still win above the default.
def _validate_llm_window_calls(value) -> None:
    """0 (UNLIMITED) .. 1e9 calls per window; negatives are a typo."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 0 or value > 1000000000:
        raise ValueError(f"must be 0..1000000000, got {value!r}")


register(
    "llm.window_max_calls",
    int,
    _ce_unlimited(1200),
    validator=_validate_llm_window_calls,
    group="llm",
    description="Per-tenant billable-LLM calls per window "
                "(services/llm_rate_limit.py window cap; 429 above this). "
                "0 means UNLIMITED on "
                "CE — CE has no quantity caps. The LLM_RATE_LIMIT_WINDOW_"
                "CALLS env var and the platform/tenant DB rows still "
                "override.",
)
register(
    "llm.assistant_window_max_calls",
    int,
    _ce_unlimited(600),
    validator=_validate_llm_window_calls,
    group="llm",
    description="Per-tenant assistant calls per window (the assistant is "
                "free, so its volume ceiling is kept separate from the "
                "billable tools). 0 means UNLIMITED on CE. LLM_RATE_LIMIT_ASSISTANT_CALLS env "
                "var and the platform/tenant DB rows override.",
)
register(
    "llm.monthly_token_quota",
    int,
    0,
    group="llm",
    description="Per-tenant monthly (prompt+completion) token quota for "
                "POST /v1/llm/chat/completions (429 when reached). "
                "0 = unlimited. CE is not metered.",
)
# 30.6.7 (c7-a3) — wake-on-demand for the tenant LLM endpoint
# (services/tenant_llm_api._wake_or_503): a request for a model the tenant's
# own Persistent Stack serves but whose stack is stopped wakes that stack
# through services/stack_wake.ensure_stack_ready, waits inline up to
# llm.model_wake_inline_wait_seconds, then 503s `model_waking` + Retry-After.
# DB-backed only, never env. The TOTAL budget (llm.model_wake_timeout_seconds)
# belongs to the CALLER node (c7-a5, the scheduler path that owns the whole
# 600 s); the tenant LLM endpoint never holds a request that long.
def _validate_llm_wake_int_range(minimum: int, maximum: int, field: str):
    def _check(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) \
                or not (minimum <= value <= maximum):
            raise ValueError(
                f"{field} must be an integer between {minimum} and "
                f"{maximum} (got {value!r})")
    return _check


register(
    "llm.model_wake_timeout_seconds",
    int,
    600,
    group="llm",
    validator=_validate_llm_wake_int_range(30, 3600,
                                           "llm.model_wake_timeout_seconds"),
    description="TOTAL seconds a caller may spend getting a stopped model "
                "stack running before it gives up (30.6.7 c7-a5, the "
                "scheduler/caller node that owns the whole wake budget). "
                "DB-backed, never env. Default 600.",
)
register(
    "llm.model_wake_inline_wait_seconds",
    int,
    25,
    group="llm",
    validator=_validate_llm_wake_int_range(
        0, 55, "llm.model_wake_inline_wait_seconds"),
    description="Seconds POST /v1/llm/chat/completions waits INLINE for a "
                "stopped model stack to come up before answering 503 "
                "model_waking + Retry-After (30.6.7 c7-a3). Capped at 55 so "
                "a request is never held for the full "
                "llm.model_wake_timeout_seconds; 0 = never wait inline, "
                "wake then 503. DB-backed, never env. Default 25.",
)
register(
    "llm.wake_wait_max_concurrent",
    int,
    # FIX-CE-UNLIMITED-LLM-WAKE-WAIT: CE = 0 (no cap; the consumer never
    # refuses on 0), paid = 8. Probe error fails to the PAID value.
    _ce_unlimited(8),
    group="llm",
    validator=_validate_llm_wake_int_range(
        0, 64, "llm.wake_wait_max_concurrent"),
    description="Max tenant-LLM requests that may be waiting on a stack "
                "wake at once (30.6.7 c7-a3); above this the endpoint does "
                "NOT wake anything and answers 503 model_waking right away "
                "so a stopped stack can't pile up request threads. DB-backed, "
                "never env. 0 = no cap (default).",
)
# GX.S8 — app notification fan-out (routers/notifications.py
# POST /v1/notifications). Limits enforced in
# services/app_notifications.fan_out (tenant row wins over these
# defaults). Never env.
register(
    "notifications.max_recipients_per_call",
    int,
    _ce_unlimited(500),
    group="notifications",
    description="Max resolved in-tenant recipients (users + group members, "
                "deduped) per POST /v1/notifications call (422 above this). "
                "CE default is 0 (unlimited); paid default is 500.",
)
register(
    "notifications.rate_limit_per_minute",
    int,
    # FIX-CE-UNLIMITED-B1: CE = 0 (unlimited; consumers treat <=0 as no limit), paid = 60.
    _ce_unlimited(60),
    group="notifications",
    description="Per-tenant app-notification fan-out rate (requests/minute, "
                "Redis sliding window) for POST /v1/notifications "
                "(429 above this).",
)
# GX.S6 — per-stack/app storage scopes (routers/storage_scopes.py).
# Tunables for POST /v1/storage/scopes/{kind}/{id}/links + GET/DELETE
# .../objects. Never env.
register(
    "storage_scopes.link_max_ttl_seconds",
    int,
    3600,
    group="storage_scopes",
    description="Max TTL (seconds) a presigned storage-scope link may be "
                "issued for (requested TTLs above this are capped). "
                "Default 3600 (1h).",
)
register(
    "storage_scopes.max_put_size_bytes",
    int,
    5 * 1024 * 1024 * 1024,
    group="storage_scopes",
    description="Tenant-facing max object size (bytes) for PUT links from "
                "storage scopes. Surfaced on the issued link so clients can "
                "pre-validate; S3 itself caps a single PUT at 5GiB. "
                "Default 5GiB.",
)
register(
    "storage_scopes.list_page_size",
    int,
    100,
    group="storage_scopes",
    description="Default + max page size for GET "
                "/v1/storage/scopes/{kind}/{id}/objects listings. "
                "Default 100.",
)
# GX.S7 — scheduled HTTP triggers (routers/http_triggers.py,
# services/http_triggers.py). Tunables for the signed cron/interval HTTP
# calls fired at Persistent Stack / App targets. Never env.
register(
    "triggers.timeout_seconds",
    int,
    10,
    group="triggers",
    description="httpx timeout (seconds) for each outbound HTTP trigger "
                "call. A call still in flight past this is a timeout "
                "failure (counted + retried on the next fire). "
                "Default 10.",
)
register(
    "triggers.max_per_tenant",
    int,
    _ce_unlimited(50),
    group="triggers",
    description="Max live (non-deleted) HTTP triggers per tenant "
                "(422 above this at create). 0 = no limit (default). An explicit row wins.",
)
register(
    "triggers.failure_alert_threshold",
    int,
    3,
    group="triggers",
    description="Consecutive-fire failure count (N) at which a trigger "
                "alerts through all three channels (MOD notification, "
                "email, outbound webhook). 0 disables alerting. "
                "Default 3.",
)
register(
    "triggers.min_interval_seconds",
    int,
    60,
    group="triggers",
    description="Minimum interval (seconds) between fires for an "
                "interval-kind trigger, and the floor applied to a "
                "cron-computed next fire (422 below this at create). "
                "Default 60.",
)
# GX.S7 — MOD -> stack/app caller identity for HTTP trigger calls. When on,
# each fire AT a stack-service or app target additionally sends an RS256
# short-TTL (300 s) "MOD trigger call" token (typ mod-trigger-call-v1,
# utils/build_task_token.mint_trigger_call_token) as
# `Authorization: Bearer <token>`, IN ADDITION TO the per-trigger HMAC headers
# (which are always sent — existing receivers keep working). Off = HMAC-only.
# Same pattern as stacks.identity_token_enabled (GX.S3): DB setting, never env.
register(
    "http_triggers.identity_signing_enabled",
    bool,
    True,
    group="triggers",
    description="Send a signed MOD caller-identity token (Authorization: "
                "Bearer <mod-trigger-call-v1 JWT>) alongside the HMAC headers "
                "on HTTP trigger fires to stack-service/app targets. Off = "
                "HMAC headers only. Default True.",
)
# GX.S10a — stack variables + secrets. Size caps for the file kinds, DB-backed
# (tunables are settings, never env). services/stack_variables reads both.
register(
    "stacks.secret_file_max_bytes",
    int,
    1 * 1024 * 1024,
    group="stacks",
    description="Max bytes for a secret_file variable value (and for a "
                "from_cog_id snapshot copied into Vault at save time; 422 "
                "above this). Default 1 MiB.",
)
# GT.C2 — drain mode. When True, the platform stops dispatching NEW work:
# the worker claim SQL refuses claims, the orchestration consumer holds
# queued runs (does not fail them), schedule triggers skip their tick without
# advancing next_fire (a missed cron fires ONCE on resume), and Execute
# returns 503. In-flight work is untouched — it finishes. Set by
# POST /v1/system/drain, cleared by POST /v1/system/resume. Read on the
# hot claim path through this module's cached getter (never a per-claim
# DB round trip once the TTL cache holds the value).
register(
    "dispatch.paused",
    bool,
    False,
    group="ops",
    description="Drain mode (GT.C2): when true, no new task claims, no new "
                "orchestration dispatch, no schedule-trigger fires, and "
                "Execute returns 503. In-flight runs/tasks are unaffected "
                "and finish normally. Toggled by POST /v1/system/drain and "
                "POST /v1/system/resume; GET /v1/system/drain-status reports "
                "in-flight counts. Default False (dispatch on).",
)
register(
    "stacks.storage_file_max_bytes",
    int,
    2 * 1024 * 1024 * 1024,
    group="stacks",
    description="Max bytes for a storage_file variable (a tenant asset "
                "mounted read-only into a stack; 422 above this at save and "
                "at deploy). Default 2 GiB.",
)
# GX.S3 — stack identity tokens. Every stack container gets an auto-injected,
# short-lived, auto-rotated token (as the stack/app service user, scoped to
# its own app/stack) to call the MOD API, delivered as a file secret
# (mod_identity_token at /run/secrets/mod_identity_token) — never env.
# Rotation is redeploy-driven until the GX.S3b in-place refresh op lands, so
# the TTL must outlive the expected redeploy cadence.
# STACK-CALL-CALLER-IDENTITY slice 1 (owner 11-25: Q-1 off by default). When on,
# the MOD-minted `mod-trigger-call-v1` token of a through-MOD stack call also
# carries WHO called: sub ("user:<id>"), sub_kind (user/api_key/service), via,
# key_id (API-key calls only), stack_id, service, scope "caller.identity".
# Never name or email. Identity only: no on-behalf-of power. The per-stack
# admin opt-in (slice 2, needs a migration) will narrow this; until then this
# is the platform-wide switch. Shared code: CE + Paid.
register(
    "stacks.call_identity_claim_enabled",
    bool,
    False,
    group="stacks",
    description="Add the caller's user id (sub), kind, channel and stack/"
                "service pins to the token MOD sends a stack on a call "
                "through MOD (/v1/stacks/{id}/services/{svc}/call/...). "
                "No name or email is ever included. Default False. "
                "CE + Paid.",
)
register(
    "stacks.identity_token_enabled",
    bool,
    True,
    group="stacks",
    description="Inject a stack identity token file secret (mod_identity_token) "
                "into every service of every deployed stack. Off = no "
                "injection (stacks fall back to whatever credential they "
                "already carry). Default True.",
)
register(
    "stacks.identity_token_ttl_seconds",
    int,
    48 * 60 * 60,
    group="stacks",
    description="TTL of a minted stack identity token. Rotated in place at 50% "
                "TTL (c8-A2) for workers advertising secret_refresh; older "
                "workers are redeployed/alerted before expiry. Default 172800 "
                "(48h, owner Q-C8 = B); max 7 days.",
)
register(
    "stacks.identity_rotation_enabled",
    bool,
    True,
    group="stacks",
    description="In-place stack identity rotation (30.6.8 c8-A2): the hourly "
                "tick re-mints and pushes via the worker's refresh_secret_files "
                "op at 50% TTL. Default True.",
)
# 30.6.3 (c3-J1) — per-stack DEPLOY tokens (the admin-minted, revocable
# credential a stack owner hands out so CI/agents can redeploy their own stack
# without a full-permission key). DB-backed only, never env; POST /v1/api-keys
# with `stack_id` (c3-J2) reads the first four, and every deploy that sends
# `image_overrides` reads the registry allow through `load_registry_allow`. A
# mint's ttl_days is capped at stacks.deploy_token_ttl_days_max (0 = not
# allowed), so a minted token always expires.
register(
    "stacks.deploy_token_enabled",
    bool,
    True,
    group="stacks",
    description="Stack-pinned API keys (Settings > API keys > Stack deploy) are "
                "mintable. Off = minting returns 403 deploy_tokens_disabled; "
                "existing keys keep working until revoked. Default True.",
)
register(
    "stacks.deploy_token_ttl_days_default",
    int,
    90,
    group="stacks",
    description="Default TTL (days) for a stack-pinned API key when the mint body "
                "omits ttl_days. Capped at "
                "stacks.deploy_token_ttl_days_max. Default 90.",
)
register(
    "stacks.deploy_token_ttl_days_max",
    int,
    365,
    group="stacks",
    description="Upper bound (days) on a stack-pinned API key TTL; a mint "
                "asking above this is a 422 invalid_ttl (NOT clamped). 0 is never "
                "allowed. Default 365.",
)
register(
    "stacks.deploy_token_max_per_stack",
    int,
    _ce_unlimited(5),
    group="stacks",
    description="Max live (not soft-deleted) stack-pinned keys one stack may hold; a "
                "mint past this is a 409 deploy_token_limit. 0 = no limit (default). An explicit row wins.",
)
register(
    "stacks.deploy_token_registry_allow",
    list,
    [],
    group="stacks",
    description="Tenant registry allowlist for deploy image_overrides. "
                "Fail-closed: empty means no overrides allowed. Each entry "
                "is a registry/repo-prefix; match is on path-component boundary "
                "(e.g. 'ghcr.io/acme' allows 'ghcr.io/acme/x' but NOT "
                "'ghcr.io/acme-evil'). This applies to every deploy that sends "
                "`image_overrides`, whoever calls (human session or stack-pinned "
                "key).",
)
# CONSIGN §0.1 — allowed values for stacks.tls_issuer. The empty string is the
# OFF/INERT edge; the three issuers are exactly what scripts/caddy-entrypoint.sh
# accepts (its `case "${STACK_TLS_ISSUER:-}"` arm), so a typo cannot silently
# render a half-configured 443 listener.
_STACK_TLS_ISSUER_VALUES = ("", "letsencrypt", "vault-acme", "byo")


def _validate_stack_tls_issuer(value: str) -> None:
    if value not in _STACK_TLS_ISSUER_VALUES:
        raise ValueError(
            f"stacks.tls_issuer must be one of "
            f"{list(_STACK_TLS_ISSUER_VALUES)}, got {value!r}"
        )
    if value == "byo":
        # FIX-CE-HIDE-BYO (FIND-2, owner verdict A): CE refuses the byo issuer —
        # no certificate is ever loaded in CE (caddy-entrypoint.sh:444-466, the
        # cert-watch sidecar is not shipped). Guard is INSIDE `if is_ce_edition()`
        # so paid keeps its original values, wording and behaviour.
        from utils.paid_import import is_ce_edition
        if is_ce_edition():
            raise ValueError(
                "stacks.tls_issuer 'byo' is not available in this edition; "
                "use 'letsencrypt' or 'vault-acme'"
            )


# CONSIGN §0.1 (go-live prerequisite) — the stack-ingress TLS issuer. It used to
# be a bootstrap env var (STACK_TLS_ISSUER), which meant switching the edge ON
# (or between issuers) needed an API container recreate. It is now this DB-backed
# setting, read at CALL time by utils/caddy_cert_lifecycle.py (the byo cert-watch
# sidecar) and by scripts/caddy-entrypoint.sh's consumers, so seeding a platform
# row switches it live. The env_fallback is the migration bridge ONLY: with no
# platform row the old env value still wins, so a deployment that has not seeded
# the setting behaves byte-identically to today.
register(
    "stacks.tls_issuer",
    str,
    "",
    env_fallback="STACK_TLS_ISSUER",
    restart_required=False,
    validator=_validate_stack_tls_issuer,
    group="stacks",
    description="TLS issuer for the stack-ingress Caddy edge: '' (default) = "
                "edge OFF/INERT (no 443 listener, no on-demand issuance); "
                "'letsencrypt' | 'vault-acme' = certmagic owns the whole cert "
                "lifecycle (auto-renew, no Vault read by the watcher); 'byo' = "
                "the operator's own cert+key are read from Vault and hot-reloaded "
                "by the cert-watch sidecar. Seeding a platform row switches the "
                "edge live with no container recreate (CONSIGN §0.1).",
)
# 14.14 — k8s per-task peak-VRAM read-back (services.k8s_task_metrics
# .readback_for_completed_task): the Prometheus read timeout for the
# post-completion fetch. Best-effort by design — a slow Prometheus must
# never stall task completion, so the connect timeout stays capped at 3s.
register(
    "k8s_task_metrics.prometheus_timeout_s",
    float,
    8.0,
    group="worker",
    description="Read timeout (seconds) for the 14.14 k8s VRAM read-back's "
                "Prometheus query (connect timeout is min(3, this)). Default "
                "8.0; clamped to 1..30 at use.",
)
# GY.C0 — declared compliance environment (multi-select). MOD *enables*
# the client's compliance work; it never provides, certifies or claims
# compliance. Declaring an environment records intent + lists the controls
# that environment needs (services/compliance_environments.py, static
# versioned map); on CE nothing is enforced. Default [] (unset) —
# answering is never required.
def _validate_declared_environment(value: list) -> None:
    # Lazy import: compliance_environments imports this module at
    # module scope, so importing it at platform_settings import time
    # would be a cycle.
    # FX.162.p5 — compliance is paid-only (owner Q157): the CE carve strips
    # services/compliance_environments.py, so the vocabulary check is skipped
    # there (CE fallback = no declared-environment enforcement).
    from utils.paid_import import optional_paid
    cenv = optional_paid("services.compliance_environments")
    if cenv is None:
        return
    bad = [v for v in value if v not in cenv.ALLOWED_ENVIRONMENTS]
    if bad:
        raise ValueError(
            f"declared environment(s) not recognized: {sorted(bad)!r} — "
            f"allowed values: {list(cenv.ALLOWED_ENVIRONMENTS)}")


register(
    "compliance.declared_environment",
    list,
    [],
    group="compliance",
    validator=_validate_declared_environment,
    description="GY.C0 — which compliance environment(s) this install runs "
                "in (multi-select: none / cmmc / fedramp_moderate / "
                "fedramp_high_il5 / iso27001 / soc2 / tpn / privacy). "
                "Declaring records intent and lists the needed controls; "
                "it does not configure or enforce anything on CE and is "
                "never a compliance claim. Default [] (unset).",
)
# GY.C1 — active compliance PROFILES (multi-select). Unlike the declared
# environment (intent + needs list), an ACTIVE profile on a paid edition
# LOCKS the settings it requires: weakening one of them through this
# registry's write path returns 409 + an audit event
# (services/compliance_profiles.py). On CE the list is informational
# only (needs list) — nothing is enforced.
def _validate_compliance_profiles(value: list) -> None:
    # FX.162.p5 — CE carve: no compliance profile vocabulary -> no validation.
    from utils.paid_import import optional_paid
    cprof = optional_paid("services.compliance_profiles")
    if cprof is None:
        return
    bad = [v for v in value if v not in cprof.ALLOWED_PROFILES]
    if bad:
        raise ValueError(
            f"compliance profile(s) not recognized: {sorted(bad)!r} — "
            f"allowed values: {list(cprof.ALLOWED_PROFILES)}")


register(
    "compliance.profiles",
    list,
    [],
    group="compliance",
    validator=_validate_compliance_profiles,
    description="GY.C1 — which compliance PROFILES are active on this "
                "install (multi-select: cmmc / fedramp_moderate / "
                "fedramp_high_il5 / iso27001 / soc2 / tpn / privacy). "
                "Each active profile locks the platform settings it "
                "requires (services/compliance_profiles.py "
                "PROFILE_REQUIRED_SETTINGS): weakening a locked setting "
                "is not blocked in Community Edition; the profiles are informational only. Default [] (none).",
)
# GY.C0 — grace window (days) for DISRUPTIVE compliance-profile changes
# (forced MFA enrolment, session bounds, lockout, inactive-disable)
# applied via POST /v1/platform/compliance/profiles/apply. The deadline
# is recorded (compliance.grace_deadlines + audit), not auto-enforced.
register(
    "compliance.disruptive_grace_days",
    int,
    14,
    validator=_validate_non_negative_int,
    group="compliance",
    description="GY.C0 — how many days of grace disruptive compliance-"
                "profile changes get (forced MFA enrolment, session "
                "limits, lockout, inactive-disable). The deadline is "
                "recorded in compliance.grace_deadlines and the apply "
                "audit rows; nothing auto-enforces at the deadline. "
                "Default 14.",
)
# GY.C0 — where disruptive-item grace deadlines are RECORDED: setting
# key -> ISO-8601 deadline. Informational + audited; there is no
# platform MFA-enrolment-deadline enforcement setting yet (Keycloak
# queues CONFIGURE_TOTP at login — follow-up GY.C3).
register(
    "compliance.grace_deadlines",
    dict,
    {},
    group="compliance",
    description="GY.C0 — recorded grace deadlines for disruptive "
                "compliance-profile changes (setting key -> ISO-8601 "
                "deadline). Written by the preview->apply flow; "
                "surfaced in /v1/platform/compliance/profiles + "
                "/status. Never auto-enforced.",
)
# GSEC R-007 (SEC-6-001) — zip workflow-import caps. `POST /workflows/import-zip`
# extracts tenant-supplied `.py` DAG files, so a hostile archive (zip bomb /
# entry flood) must be rejected before any file touches disk. The total
# UNCOMPRESSED bytes are the meaningful bound (compressed ratio is unbounded),
# enforced entry-by-entry while reading, so we never inflate past the cap.
def _validate_positive_int(value: int) -> None:
    if value <= 0:
        raise ValueError(f"{value!r} must be a positive integer")


register(
    "workflow_import.zip_max_uncompressed_bytes",
    int,
    64 * 1024 * 1024,
    group="workflows",
    validator=_validate_positive_int,
    description="GSEC R-007 — max TOTAL uncompressed bytes across all entries "
                "a workflow-import ZIP may carry. Reading aborts (422) the "
                "moment the running sum crosses it. Default 64 MiB.",
)
register(
    "workflow_import.zip_max_entries",
    int,
    500,
    group="workflows",
    validator=_validate_positive_int,
    description="GSEC R-007 — max number of entries a workflow-import ZIP may "
                "contain. Exceeding it is rejected (422). Default 500.",
)
# GSEC R-002 (Critical) — license-issuer trust. ensure-license-issuer-keypair.sh
# mints an RSA-4096 issuer pair on the customer's OWN box, and the public half
# rides into the API env as LICENSE_ISSUER_PUBLIC_KEY_B64. While the verifier
# trusted that key unconditionally, ANY install could self-mint a
# full-capability, never-expiring license that its own API accepted. The
# verifier (utils/license_verify.py) now trusts the local auto-generated
# issuer key ONLY while this setting is true; pinned vendor keys
# (utils/license_trust/*.pem) + operator-explicit trust vars are always
# trusted, and a no-expiry license additionally requires a pinned vendor
# signature. Default False = secure for new installs. The one-shot startup
# seed (services/platform_settings_seed.py) writes True ONLY when the install
# already carries a valid local-issuer-signed license, so existing installs
# keep working until the lead flips this to False with the vendor key +
# vendor-signed license in place.
register(
    "licensing.trust_local_issuer",
    bool,
    False,
    group="licensing",
    description="GSEC R-002 — trust the locally auto-generated license-issuer "
                "key (LICENSE_ISSUER_PUBLIC_KEY_B64) when verifying signed "
                "licenses. Default False (secure): only pinned vendor keys "
                "(utils/license_trust/*.pem) and operator-explicit trust vars "
                "are trusted, and no-expiry licenses require a pinned vendor "
                "signature. The startup seed flips this to True automatically "
                "only for installs that already have a valid local-issuer-"
                "signed license; flip it back to False once the vendor key + "
                "vendor-signed license are installed.",
)
# ---------------------------------------------------------------------------
# GSEC fix-wave FX.12 — Keycloak/session hardening (R-009/R-010/R-043).
#
# keycloak-init.sh converges the realm on every run but cannot read this
# registry (it runs inside the Keycloak container before the API is up), so
# the numeric values below are PINNED in CORE/startup/keycloak-init.sh and
# must be kept in sync. They are registered here so operators can SEE the
# effective auth-policy values (and their NIST rationale) in the admin
# platform-settings page instead of digging through the init script.
# ---------------------------------------------------------------------------
register(
    "auth.sso_session_idle_seconds",
    int,
    3600,
    group="auth",
    description="GSEC R-009 / FX.54 (owner Q49=A) — SSO (online) session idle "
                "timeout in seconds for the MOST PERMISSIVE tier the realm "
                "serves, i.e. the human-USER tier: 60 min of inactivity. "
                "Pinned in CORE/startup/keycloak-init.sh as "
                "ssoSessionIdleTimeout (realm + client-level overrides on the "
                "dashboard/workflow-builder clients). Tighter tiers are NOT "
                "realm values: gov/DoD compliance profiles lock 900 s "
                "(15 min) via PROFILE_REQUIRED_SETTINGS, and the admin tier "
                "(auth.admin_sso_session_idle_seconds, 1800 s) is not a realm "
                "value (enforced server-side on the auth path, FX.54 part 2).",
)
register(
    "auth.sso_session_max_lifetime_seconds",
    int,
    43200,
    group="auth",
    description="GSEC R-009 / FX.54 (owner Q49=A) — SSO (online) session max "
                "lifetime in seconds for the human-USER tier: 12 h. Pinned in "
                "CORE/startup/keycloak-init.sh as ssoSessionMaxLifespan "
                "(realm + client-level overrides). gov/DoD profiles lock "
                "28800 s (8 h); the admin tier is "
                "auth.admin_sso_session_max_lifetime_seconds (28800 s).",
)
# FX.54 (owner Q37=A + Q49=A) — the session tiers. The realm can only carry
# ONE idle/max pair, so it carries the MOST PERMISSIVE tier it serves (the
# user tier above); every tighter tier is either a compliance-profile lock
# (gov/DoD 15 min / 8 h, see services/compliance_profiles.py) or a per-role
# server-side limit (the admin pair below; enforced on the auth path by
# services/session_tier_enforcement.py, FX.54 part 2). Remember-me is a realm-level
# toggle + its own idle/max pair (7 days, owner Q49=A "remember-me <= 7 days");
# gov/DoD profiles force it off.
register(
    "auth.admin_sso_session_idle_seconds",
    int,
    1800,
    validator=_validate_non_negative_int,
    group="auth",
    description="FX.54 — admin-tier SSO idle timeout in seconds (30 min). "
                "Applies to humans in the platform_admin Keycloak group. NOT "
                "a realm value (the realm carries the user tier); enforced "
                "server-side on the auth path (FX.54 part 2, "
                "services/session_tier_enforcement.py).",
)
register(
    "auth.admin_sso_session_max_lifetime_seconds",
    int,
    28800,
    validator=_validate_non_negative_int,
    group="auth",
    description="FX.54 — admin-tier SSO max lifetime in seconds (8 h). "
                "Enforced server-side on the auth path (FX.54 part 2, "
                "services/session_tier_enforcement.py).",
)
register(
    "auth.remember_me_enabled",
    bool,
    True,
    group="auth",
    description="FX.54 — Keycloak realm rememberMe toggle. ON by default for "
                "the user tier (a human may opt in to a 7-day session via "
                "auth.remember_me_max_lifetime_seconds); gov/DoD compliance "
                "profiles (cmmc, fedramp_moderate, fedramp_high_il5) force it "
                "OFF so every session is bounded by the tier idle/max pair.",
)
register(
    "auth.remember_me_idle_seconds",
    int,
    604800,
    validator=_validate_non_negative_int,
    group="auth",
    description="FX.54 — ssoSessionIdleTimeoutRememberMe in seconds (7 d). "
                "Keycloak's 'Remember Me' duration pattern: the remember-me "
                "idle timeout and max lifespan are set to the SAME value.",
)
register(
    "auth.remember_me_max_lifetime_seconds",
    int,
    604800,
    validator=_validate_non_negative_int,
    group="auth",
    description="FX.54 — ssoSessionMaxLifespanRememberMe in seconds (7 d, "
                "owner Q49=A 'remember-me <= 7 days'). Ignored by Keycloak "
                "while auth.remember_me_enabled is false.",
)
register(
    "auth.access_token_lifespan_seconds",
    int,
    900,
    group="auth",
    description="GSEC R-009 — access-token lifetime in seconds (NIST 800-53 "
                "AC-12: <= 15 min). Pinned in CORE/startup/keycloak-init.sh "
                "as accessTokenLifespan. Offline/refresh tokens for "
                "long-running workflow runs are intentionally NOT bounded "
                "yet — see the offline-session note in keycloak-init.sh "
                "(requires the exchange-token persistence fix first).",
)
register(
    "auth.mfa_required_for_admins",
    bool,
    True,
    group="auth",
    description="GSEC R-010 — enforce OTP (TOTP) enrollment for users in the "
                "platform_admin Keycloak group. keycloak-init.sh queues the "
                "CONFIGURE_TOTP required action on them (idempotent: users "
                "already carrying a TOTP device are skipped); the realm's "
                "stock Conditional-OTP step in the browser flow then makes "
                "OTP mandatory on every login. Not enforced in Community Edition; users can still add their own TOTP device.",
)
register(
    "auth.mfa_required_for_all",
    bool,
    False,
    group="auth",
    description="GSEC R-010 — enforce OTP enrollment for ALL realm users "
                "(machine users and members of the e2e_service_principals "
                "exemption group are always skipped). Default OFF; flip ON "
                "for the gov/DoD buyer tier.",
)
register(
    MFA_ENFORCEMENT_ENABLED_SETTING_KEY,
    bool,
    True,
    group="auth",
    description="GSEC FX.53 (owner Q36=A) — run the API-side MFA "
                "enforcement sweep (services/mfa_enforcement.py): in-scope "
                "users without an enrolled factor (TOTP, security key or "
                "PIV/CAC) get a 7-day grace period, then the "
                "CONFIGURE_TOTP required action is queued in Keycloak so "
                "the next login forces setup. The Dashboard shows the "
                "countdown banner from GET /v1/me/mfa/enforcement. "
                "Default ON; changes trigger an immediate sweep.",
)
register(
    "auth.webauthn_enabled",
    bool,
    True,
    group="auth",
    description="GY.C3b — offer a security key (WebAuthn) as a 2FA option "
                "next to the authenticator app. keycloak-init.sh enables the "
                "WEBAUTHN_REGISTER/WEBAUTHN_AUTHENTICATE required actions, "
                "sets the WebAuthn policy (rpName MOD, userVerification "
                "preferred) and adds the Conditional WebAuthn step to the "
                "browser flow; the API keeps the live realm in step on "
                "setting changes. Service / client-credentials / direct-"
                "grant / e2e_service_principals accounts never pass the "
                "browser flow, so they are always unaffected. Default ON.",
)
register(
    "auth.phishing_resistant_only",
    bool,
    False,
    group="auth",
    description="GY.C3b — phishing-resistant-only sign-in for human users: "
                "the browser flow drops the OTP (authenticator-app) "
                "alternative and a WebAuthn security key becomes required. "
                "Enabling is refused (409) unless the acting admin already "
                "has a security key registered, so the first toggle can "
                "never lock every human login out. Service / machine "
                "accounts (client-credentials, direct grant, "
                "e2e_service_principals) are exempt — they never use the "
                "browser flow. Default OFF; gov/DoD compliance profiles "
                "(GY.C1) lock it ON.",
)
register(
    "auth.login_banner_text",
    str,
    "",
    group="auth",
    description="GSEC R-043 — AC-8 system-use notification rendered on the "
                "Keycloak login page (modtech login theme). Empty = no "
                "banner. keycloak-init.sh pushes this value into the login "
                "theme property themes.login.headerHtml (newlines become "
                "<br>); on a live realm, update the value and re-apply via "
                "the FX.12 manual steps.",
)
# GSEC R-096 (FX.30) — setting-driven login (brute-force) lockout. The
# four keys drive the Keycloak realm brute-force fields (bruteForceProtected
# is always ON; failureFactor / maxDeltaTimeSeconds /
# maxFailureWaitSeconds / permanentLockout follow the settings).
# keycloak-init.sh keeps its baseline EQUAL to these defaults (5 failures
# + temporary backoff) and re-converges on boot; the after-commit hook
# (services/lockout_policy.converge_lockout) pushes live changes to the
# realm without a restart. The gov/DoD secure-configuration profile
# (fedramp_high_il5) requires 3 / 15 / 30 + admin unlock.
def _validate_lockout_int_range(minimum: int, maximum: int,
                                field: str):
    def _check(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) \
                or not (minimum <= value <= maximum):
            raise ValueError(
                f"{field} must be an integer between {minimum} and "
                f"{maximum} (got {value!r})")
    return _check


register(
    LOCKOUT_MAX_FAILURES_SETTING_KEY,
    int,
    5,
    group="auth",
    validator=_validate_lockout_int_range(1, 20, "auth.lockout_max_failures"),
    description="GSEC R-096 — number of failed sign-ins before the "
                "account is locked (Keycloak failureFactor). Default 5, "
                "matching keycloak-init.sh's baseline. The gov/DoD "
                "secure-configuration profile (fedramp_high_il5) requires "
                "3 (3 failures in 15 min).",
)
register(
    LOCKOUT_WINDOW_MIN_SETTING_KEY,
    int,
    15,
    group="auth",
    validator=_validate_lockout_int_range(1, 1440,
                                          "auth.lockout_window_min"),
    description="GSEC R-096 — the failure-count window in minutes "
                "(Keycloak maxDeltaTimeSeconds = this x 60): failures "
                "outside the window stop counting toward the lockout. "
                "Default 15.",
)
register(
    LOCKOUT_DURATION_MIN_SETTING_KEY,
    int,
    15,
    group="auth",
    validator=_validate_lockout_int_range(1, 10080,
                                          "auth.lockout_duration_min"),
    description="GSEC R-096 — lockout duration in minutes "
                "(Keycloak maxFailureWaitSeconds = this x 60): the "
                "account is locked for this long after the failure "
                "threshold is hit. Default 15; the gov/DoD "
                "secure-configuration profile requires 30.",
)
register(
    LOCKOUT_PERMANENT_SETTING_KEY,
    bool,
    False,
    group="auth",
    description="GSEC R-096 — permanent (until admin unlock) lockout "
                "(Keycloak permanentLockout). Default False = temporary "
                "backoff, the baseline. The gov/DoD profile sets True: a "
                "3rd failure in 15 min locks the account until a "
                "platform admin unlocks it (admin security page / "
                "DELETE /v1/admin/security/locked-accounts/{id}).",
)
# GY.C3c — PIV/CAC client-certificate login (Keycloak "X509/Validate
# Username Form" authenticator, provider id auth-x509-client-username-form).
# keycloak-init.sh installs the execution DISABLED; services/x509_policy.py
# converges the live shape (ALTERNATIVE when enabled / DISABLED when off)
# on every change to any of the four keys (deferred to after the settings
# commit). The edge (nginx/Caddy) must terminate mTLS and forward the
# verified peer certificate — see docs-site/docs/security/piv-cac.md.
def _validate_x509_user_mapping(value: str) -> None:
    if value not in ("subject_email", "subject_cn", "upn_san"):
        raise ValueError(
            f"{value!r} is not a PIV/CAC user mapping — expected one of "
            "'subject_email' (RFC822 SAN), 'subject_cn' (Subject CN) or "
            "'upn_san' (X.509 UPN SAN)")


def _validate_x509_revocation(value: str) -> None:
    if value not in ("ocsp", "crl", "both", "none"):
        raise ValueError(
            f"{value!r} is not a PIV/CAC revocation mode — expected one of "
            "'ocsp', 'crl', 'both' or 'none'")


register(
    X509_ENABLED_SETTING_KEY,
    bool,
    False,
    group="auth",
    description="GSEC R-043 / GY.C3c — enable PIV/CAC smart-card (X.509 "
                "client-certificate) login via the Keycloak X509/Validate "
                "Username Form authenticator in the realm browser flow "
                "(ALTERNATIVE — password+2FA stays the primary path; "
                "machine accounts never traverse the browser flow). "
                "The edge proxy must forward the verified client "
                "certificate (see docs-site security/piv-cac). Default "
                "False = the execution stays DISABLED. See "
                "keycloak_auth.x509_piv_login_enabled.",
)
register(
    X509_USER_MAPPING_SETTING_KEY,
    str,
    "subject_email",
    validator=_validate_x509_user_mapping,
    group="auth",
    description="GY.C3c — which field of the PIV/CAC certificate is "
                "matched against the MOD user's username/email in the "
                "realm: 'subject_email' (RFC822 SAN — default), "
                "'subject_cn' (Subject Common Name) or 'upn_san' (X.509 "
                "UPN SAN).",
)
register(
    X509_REVOCATION_SETTING_KEY,
    str,
    "ocsp",
    validator=_validate_x509_revocation,
    group="auth",
    description="GY.C3c — revocation checking for PIV/CAC client "
                "certificates: 'ocsp' (default), 'crl' (CRLDP), 'both' "
                "or 'none'.",
)
register(
    X509_OCSP_RESPONDER_URI_SETTING_KEY,
    str,
    "",
    group="auth",
    description="GY.C3c — OCSP responder URI used for PIV/CAC "
                "client-certificate revocation checks (empty = the "
                "certificates' own AIA OCSP URL).",
)
def _validate_audit_chain_nonneg_int(value: int) -> None:
    if value < 0:
        raise ValueError(f"{value!r} must be a non-negative integer")


register(
    "audit.max_clock_skew_ms",
    int,
    1000,
    group="audit",
    validator=_validate_audit_chain_nonneg_int,
    description="GY.C5 (NIST AU-8) — per-worker clock skew tolerated before "
                "the time-sync status reports degraded, in milliseconds. "
                "Audit timestamps are only trustworthy while clocks are "
                "disciplined: skew = server_received_at - worker_sent_at "
                "beyond this limit (or a host STA_UNSYNC) = degraded. "
                "Default 1000.",
)
# GSEC Q111 part 1 (owner decision Q111=A) — the scheduled half of the same
# NIST AU-8 control. audit.max_clock_skew_ms is the report threshold read by
# services/time_sync_status.py; time.max_clock_skew_seconds is the alert
# threshold read by services/clock_skew_monitor.py on its scheduler tick.
def _validate_time_max_clock_skew_seconds(value: int) -> None:
    if value < 1:
        raise ValueError(
            f"time.max_clock_skew_seconds must be >= 1 second, got {value!r}")


register(
    "time.max_clock_skew_seconds",
    int,
    5,
    group="monitoring",
    validator=_validate_time_max_clock_skew_seconds,
    description="GSEC Q111 — clock drift tolerated before the scheduled "
                "monitor raises an admin alert (services/clock_skew_monitor.py, "
                "tick cadence time.check_interval_minutes). Measured from the "
                "worker's own heartbeat timestamp where one exists, and from "
                "the DB clock vs the API clock otherwise. Fires once on the "
                "clear->breach transition and once on recovery. Default 5.",
)
def _validate_time_check_interval_minutes(value: int) -> None:
    if value < 1:
        raise ValueError(
            f"time.check_interval_minutes must be >= 1 minute, got {value!r}")


register(
    "time.check_interval_minutes",
    int,
    10,
    group="monitoring",
    validator=_validate_time_check_interval_minutes,
    description="GSEC Q111 — minutes between clock-drift checks "
                "(services/clock_skew_monitor.py). The scheduler job runs as a "
                "1-minute heartbeat throttled to this value, so a change takes "
                "effect on the next heartbeat without a restart. Default 10.",
)
def _validate_tls_cert_expiry_warn_days(value: int) -> None:
    if value < 1:
        raise ValueError(
            f"tls.cert_expiry_warn_days must be >= 1 day, got {value!r}")


register(
    "tls.cert_expiry_warn_days",
    int,
    21,
    group="monitoring",
    validator=_validate_tls_cert_expiry_warn_days,
    description="GSEC Q111 — a served or internal certificate whose not_after "
                "is inside this many days raises one admin alert "
                "(services/cert_expiry_monitor.py). Read-only: MOD supports "
                "renewal, this check never renews or rotates anything. "
                "Default 21.",
)
register(
    "tls.monitor_hostnames",
    str,
    "",
    group="monitoring",
    description="GSEC Q111 — comma-separated public hostnames whose served "
                "certificates the expiry monitor probes (empty = hosts derived "
                "from the existing URL settings, never hardcoded). Internal CA "
                "and leaf certs under the startup tree are always read too.",
)
def _validate_crypto_max_key_age_days(value: int) -> None:
    if value < 1:
        raise ValueError(
            f"crypto.max_key_age_days must be >= 1 day, got {value!r}")


register(
    "crypto.max_key_age_days",
    int,
    365,
    group="crypto",
    validator=_validate_crypto_max_key_age_days,
    description="GY.C6a — the age a key/certificate is allowed to reach "
                "before the conformance check crypto.key_rotation_age warns "
                "that rotation evidence is overdue (Vault transit keys and "
                "internal TLS / PG TLS certs; a cert whose not_after is "
                "inside crypto.cert_expiry_warn_days, fixed at 30 days, is "
                "warned too). Reporting only: MOD supports rotation, it "
                "never rotates anything here. Default 365.",
)


register(
    "crypto.fips_mode_required",
    bool,
    False,
    group="crypto",
    description="GY.C2a — whether this install is REQUIRED to run in FIPS "
                "mode. While false the crypto-status report and the boot "
                "known-answer self-test are informational (a stock non-FIPS "
                "install is not unhealthy and the conformance check is "
                "not-applicable). While true, a failed self-test or a "
                "non-enforced build marks readiness unhealthy and fails the "
                "crypto.fips_mode check. Reporting only: MOD supports FIPS "
                "mode operation, this setting never installs a FIPS provider "
                "and nothing here certifies compliance. Default false.",
)

# ---------------------------------------------------------------------------
# FX.117 PART 2 (owner decision Q104=A) — customer secret store: optional
# expiry + reminders (group "secrets").
#
# Enforcement lives in services/secrets_service.py:
#   * expiry_reminder_days — the secret owner (created_by_user_id) is
#     reminded N days before each secret's expires_at (deduped via
#     secrets.expiry_reminders_sent); the daily scheduler job
#     (services/scheduler.py FX.117 block, 04:32 UTC) sends the reminders
#     and stamps expired_notified_at once past expiry — an expired
#     secret's run-time injection is refused at the read path
#     (403 + audited SECRET_ACCESS_DENIED reason=secret_expired).
#     NEVER a hard delete (ISO 27001: soft state only).
#   * max_secret_age_days  — the ceiling on how far out an explicit
#     expires_at may be (PATCH /secrets/{id} rejects beyond it, 400).
#
# No env vars: tenant-scoped DB platform settings.
# ---------------------------------------------------------------------------
def _validate_fx117p2_reminder_days(value: list) -> None:
    for d in value:
        if not isinstance(d, int) or isinstance(d, bool) or d < 0:
            raise ValueError(
                "secrets.expiry_reminder_days must be a list of "
                f"non-negative ints, got {value!r}")


def _validate_fx117p2_max_age(value: int) -> None:
    if not (1 <= int(value) <= 3650):
        raise ValueError(
            "secrets.max_secret_age_days must be in 1..3650, got "
            f"{value!r}")


register(
    "secrets.expiry_reminder_days",
    list,
    [3, 14],
    validator=_validate_fx117p2_reminder_days,
    group="secrets",
    description="FX.117 PART 2 (Q104=A) — reminder days-before-expiry for "
                "customer secrets with an optional expires_at: the owner "
                "is reminded N days before for each N in this list "
                "(deduped per secret via secrets.expiry_reminders_sent, "
                "so each day fires at most once). Default [3, 14]. 0 in the list also notifies on the "
                "expiry day itself.",
)
register(
    "secrets.max_secret_age_days",
    int,
    365,
    validator=_validate_fx117p2_max_age,
    group="secrets",
    description="FX.117 PART 2 (Q104=A) — the maximum explicit lifetime a "
                "secret may be given (PATCH /secrets/{id} expires_at "
                "beyond now+this many days is 400). Default 365.",
)

# ---------------------------------------------------------------------------
# FX.125 PART 1 (owner decision Q112=A) — platform key rotation schedules
# (group "keys").
#
# The daily 04:52 UTC sweep (services/scheduler.py) builds the runtime
# inventory via services/key_inventory.py, ages each item against the
# schedules below, and (while keys.rotation_alert_enabled is True) writes
# one admin Notification + one KEY_ROTATION_OVERDUE audit row per overdue
# item per day. The cmmc / fedramp_high_il5 gov profiles (services/
# compliance_profiles.py PROFILE_REQUIRED_SETTINGS) tighten every bound.
# Reporting only: nothing in part 1 rotates anything — the rotation jobs
# themselves are part 2. No env vars: DB platform settings only.
# ---------------------------------------------------------------------------
def _validate_keys_rotation_days(value: int) -> None:
    if not (1 <= int(value) <= 3650):
        raise ValueError(
            "keys.*_rotation_days must be in 1..3650, got "
            f"{value!r}")


register(
    "keys.signing_rotation_days",
    int,
    365,
    validator=_validate_keys_rotation_days,
    group="keys",
    description="FX.125 (Q112=A) — maximum age (days) before a platform "
                "signing key (Keycloak realm keys, license/relay/build "
                "issuer keypairs) is due for rotation. The 04:52 UTC "
                "sweep flags overdue items; it never rotates. Default 365; "
                "gov profiles require <=180.",
)
register(
    "keys.transit_rotation_days",
    int,
    365,
    validator=_validate_keys_rotation_days,
    group="keys",
    description="FX.125 (Q112=A) — maximum age (days) of the LATEST Vault "
                "transit key version (its created_at) before the key is "
                "due for rotation. Default 365; gov profiles require <=180.",
)
register(
    "keys.cert_rotation_days",
    int,
    90,
    validator=_validate_keys_rotation_days,
    group="keys",
    description="FX.125 (Q112=A) — maximum age (days) of an internal-tls "
                "leaf certificate before it is due for re-issue (the "
                "10-year CA anchor is excluded; cert_expiry_warn_days from "
                "group 'tls' governs the near-expiry warning separately). "
                "Default 90; gov profiles require <=30.",
)
register(
    "keys.db_password_rotation_days",
    int,
    90,
    validator=_validate_keys_rotation_days,
    group="keys",
    description="FX.125 (Q112=A) — maximum age (days) before the bootstrap "
                "database role password is due for a manual re-roll. The "
                "dynamic Vault api-readwrite roles self-rotate at 1h TTL "
                "and are not part of this bound. Default 90; gov profiles "
                "require <=30.",
)
register(
    "keys.enrollment_token_rotation_days",
    int,
    30,
    validator=_validate_keys_rotation_days,
    group="keys",
    description="FX.125 (Q112=A) — maximum age (days) since the newest "
                "worker/manager enrollment-token issue before the token "
                "pool is due for re-issue (per-token expires_at still "
                "bounds individual tokens). Default 30; gov profiles "
                "require <=7.",
)
register(
    "keys.rotation_alert_enabled",
    bool,
    True,
    group="keys",
    description="FX.125 (Q112=A) — whether the daily 04:52 UTC sweep writes "
                "an admin Notification (+ KEY_ROTATION_OVERDUE audit row, "
                "deduped per item per day) for overdue keys/certs/tokens. "
                "The inventory endpoint itself stays available either way. "
                "Default True; gov profiles require true.",
)

def _validate_non_negative_int(v: Any) -> None:
    if int(v) < 0:
        raise ValueError(
            "auth.api_key_max_ttl_days must be 0 (no ceiling) or a "
            "positive integer")


register(
    "auth.api_key_token_rate_per_minute",
    int,
    30,
    group="auth",
    description="GSEC R-020 — max requests per minute to the public "
                "API-key token exchange (POST /api-keys/token), keyed "
                "by client IP via the shared rate limiter. Default 30.",
)
register(
    "auth.api_key_max_ttl_days",
    int,
    0,
    group="auth",
    validator=_validate_non_negative_int,
    description="GY.C3a — maximum API-key lifetime in days, enforced at "
                "create/rotate: with a ceiling set (>0) every new or "
                "rotated API key must carry an expires_at within the "
                "ceiling, or the mint is refused (422). 0 (default) = "
                "no ceiling, preserving today's behaviour where keys may "
                "live forever. The service-account inventory "
                "(/v1/platform/compliance/service-accounts) flags "
                "existing keys that violate the ceiling.",
)


# --- GSEC R-014 — docs / metrics exposure ----------------------------------
# /docs, /redoc and /openapi.json are re-created as explicit routes in
# main.py and gated by `api.public_docs_enabled` (default OFF on paid, ON on CE): anonymous
# callers get 404 while disabled; authenticated platform admins can still
# open them. /metrics is gated by `api.metrics_require_auth` (default ON):
# when ON, only platform admins (bearer) or local/docker-internal scrapers
# (the in-cluster Prometheus on modtex-network, target my_fastapi_app:80 in
# startup/prometheus/prometheus.yml) may read it. No Vault scrape token
# exists yet; if a remote (non-private) scraper is ever added, introduce one.
def _public_docs_default() -> bool:
    """CE ships with the API docs ON (owner decision 11-09: the docs must work on a
    fresh CE install); every paid edition keeps the GSEC R-014 default OFF.
    Edition is fixed for the life of the process, so reading it at registration
    is correct. Any trouble reading the edition fails to the safe value (OFF)."""
    try:
        from utils.paid_import import is_ce_edition
        return bool(is_ce_edition())
    except Exception:  # noqa: BLE001 — fail closed: docs stay hidden
        return False


register(
    "api.public_docs_enabled",
    bool,
    _public_docs_default(),
    group="api",
    description="GSEC R-014 — expose the interactive API docs (/docs, /redoc) "
                "and /openapi.json to anonymous callers. ON by default on CE: disabled "
                "docs return 404 to anonymous callers but remain available to "
                "authenticated platform admins (main.py _require_docs_access). "
                "An explicit platform-settings row always wins over the default.",
)
register(
    "api.metrics_require_auth",
    bool,
    True,
    group="api",
    description="GSEC R-014 — require credentials to read /metrics. Default "
                "ON: only authenticated platform admins (bearer) or "
                "localhost/docker-internal source IPs may scrape (the "
                "in-cluster mod_prometheus reaches the API as a docker-bridge "
                "peer, so its scrape keeps working). No Vault scrape token "
                "exists yet; add one before allowing any remote (non-private) "
                "scraper.",
)

# GY.V2b (GY.V2 export-control, GSEC R-040/R-133) — US-person gating
# master switch. When True, assets classified EAR/ITAR are readable/
# downloadable/runnable only by users flagged as US persons, and tasks
# whose inputs carry those levels are claimable only by US-person-
# operated workers (worker_licenses.us_person_operated, stamped
# task_payload.export_control_level at enqueue). Default False = the
# gate is inert, so existing installs are unchanged. We ENABLE export
# control; we never certify compliance with it.

# GY.V2b — opt-in extension of the US-person gate to CUI assets. CUI
# alone is NOT export-controlled (it is federal marking/dissemination
# control), so it stays ungated unless the operator turns this on WITH
# the master switch above.


register(
    "compliance.withheld_stuck_minutes",
    int,
    15,
    group="compliance",
    description="Minutes a task may sit PENDING after a compliance claim "
                "guard (export control, residency, label pool/region) "
                "withheld it from a worker before a 'no worker satisfies "
                "the rule, it has not run' alert is raised (checked by the "
                "scheduler every 5 minutes). Default 15.",
)
register(
    "compliance.first_withhold_alert_rate_per_min",
    int,
    10,
    group="compliance",
    validator=_validate_positive_int,
    description="Platform-wide cap on first-withhold alerts per minute: at most "
                "this many 'a task is waiting because a worker does not satisfy "
                "the rule' alerts are raised platform-wide in any 60-second "
                "window (a deep queue pinned to a region with no worker would "
                "otherwise raise one per task). Refusals over the cap are NOT "
                "lost: the task keeps its waiting marker and its stuck-sweep "
                "escalation, and the next summary email carries the count and "
                "the first task ids. Minimum 1; default 10.",
)
register(
    "stacks.share_platform_hf_token",
    bool,
    False,
    group="stacks",
    description="GSEC R-065 — when True, managed-AI stacks of tenants that "
                "have no HF_TOKEN secret of their own also get the platform "
                "HuggingFace token (delivered via the stack's x-mod.secrets "
                "ref to the platform token's Vault path, resolved at deploy "
                "— never as a plain env value). Default False: the platform "
                "token is used only for platform-owned stacks (tenant 1); a "
                "normal tenant's own HF_TOKEN secret always wins.",
)
# GSEC R-012 / FX.14 — the worker-side ephemeral host-port allocation range
# for `x-mod.host_ports` entries with `host: "any"`. The Go worker resolves
# this exact registry key (internal/executor/stackhostports.go
# stackHostPortRangeTunableKey) when it is ever pushed to it; until then it
# falls back to its built-in default, which is why this default mirrors it
# byte-for-byte. The mesh ACL policy generator (services/mesh_acl.py
# stack_host_port_range_for) reads the live value so a `host: "any"` stack's
# manager dial stays granted on the range the worker actually allocates
# from.
def _validate_host_port_range(value: str) -> None:
    """Must be `lo-hi` with 1 <= lo <= hi <= 65535 — the worker's own
    parseHostPortRange contract; a malformed value would grant a port range
    no worker allocates from (or, worse, be unparseable there)."""
    s = str(value).strip()
    lo_str, sep, hi_str = s.partition("-")
    if not sep:
        raise ValueError(f"host port range {s!r} must be \"lo-hi\"")
    if not lo_str.strip().isdigit() or not hi_str.strip().isdigit():
        raise ValueError(f"host port range {s!r} must be \"lo-hi\" digits")
    lo, hi = int(lo_str.strip()), int(hi_str.strip())
    if not (1 <= lo <= hi <= 65535):
        raise ValueError(f"host port range {s!r} must be 1 <= lo <= hi <= 65535")


register(
    "worker.stack_host_port_range",
    str,
    "20000-29999",
    group="worker",
    validator=_validate_host_port_range,
    description="GSEC R-012 — allocation range for `host: \"any\"` stack "
                "host ports (worker-side; must stay \"lo-hi\"). The mesh ACL "
                "grant tag:worker:<range> tracks this value so least-"
                "privilege default-deny never severs the manager's dial into "
                "worker-allocated stack ports.",
)

# GSEC R-012 / FX.14 — mesh ACL enforcement gate. Registry default True =
# fresh installs run the generated whole-tailnet default-deny policy from day
# one (apply_acl_policy's preflight still refuses to push until the manager
# node carries the API tag, so an untagged fresh install is unaffected).
# Established installs are seeded False at startup (platform_settings_seed.
# _seed_mesh_acl_enforced) so an in-use tailnet is never auto-flipped; the
# lead then previews (GET /platform/mesh-acl/preview) and flips this to True.
# MOD_MESH_ACL_ENFORCE remains the migration-bridge env fallback (it still
# lingers in CORE/startup/.env.public, which is bootstrap-only and owned
# elsewhere); a platform-scope DB row always wins over it.
register(
    "mesh.acl_enforced",
    bool,
    True,
    env_fallback="MOD_MESH_ACL_ENFORCE",
    group="mesh",
    description="GSEC R-012 — when True, the API generates and PUTs the "
                "whole-tailnet default-deny Headscale ACL policy on every "
                "stack deploy/teardown (worker→manager :80 baseline + "
                "declared stack egress + manager→worker stack host ports). "
                "When False the tailnet stays permissive (pre-R-012 "
                "behaviour). Review the would-be policy with "
                "GET /platform/mesh-acl/preview before flipping.",
)

# STACK-CALL-MESH-RELAY-FIX — the ONE relay switch + its enrollment tag, registered
# the same way as the other mesh tunables so an admin can flip them without an
# env change. Default OFF: with the switch off, `services.mesh_acl.build_acl_policy`
# emits no `tag:mod-relay` tag owner and no relay `accept` rows, so the generated
# whole-tailnet policy is byte-identical to the pre-relay build and the next
# stack deploy on an enforcing install pushes unchanged policy text. Enabling it
# is NOT sufficient to relay a call: `services.mesh_relay.build_claim` still
# requires a site-IP worker row, a declared host port and the 0400 file secret,
# and no compose entry starts the relay container anywhere. Both keys are hidden
# on CE (their acting modules services/mesh_relay.py and services/mesh_acl.py are
# api_allow-denied from the CE extract, so the settings do nothing there).
register(
    "mesh.relay_enabled",
    bool,
    False,
    env_fallback="MOD_MESH_RELAY_ENABLE",
    group="mesh",
    description="STACK-CALL-MESH-RELAY — when True, the API may route a stack call "
                "to a stack hosted on a REMOTE worker through the mesh-relay node, "
                "and the generated default-deny ACL grants that node's tag exactly "
                "the ports stacks declare. Default False: the relay hop is inert and "
                "the generated policy carries no relay rows. Requires the relay node "
                "to exist and its HMAC key file to be present; enabling alone starts "
                "nothing.",
)

register(
    "mesh.relay_tag",
    str,
    "tag:mod-relay",
    env_fallback="MOD_MESH_RELAY_TAG",
    group="mesh",
    description="STACK-CALL-MESH-RELAY — the Headscale tag the relay node carries "
                "at enrollment. The ACL generator grants that tag and the enrollment "
                "key mint stamps it, so both resolve through this one setting; a "
                "blank value falls back to the default rather than emitting a rule "
                "that resolves to nothing.",
)

# GSEC R-015/R-016 / FX.15 — privacy-erasure grace window. The daily
# privacy_erasure sweep (services/scheduler.py::_run_privacy_erasure)
# erases the object-store bytes + crypto-shreds the tenant key of users /
# tenants soft-deleted at least this many days ago. DB rows are NEVER
# deleted (ISO 27001 — no hard deletes; audit FKs + append-only audit
# tables stay intact) — only bytes + keys go, and every purge is recorded
# in audit_events. BYO-S3 tenants are report-only (customer buckets are
# never touched). The sweep runs in ISO mode too, since it deletes no rows.
register(
    "privacy.erasure_grace_days",
    int,
    30,
    group="privacy",
    description="GSEC R-015/R-016 — days after a user's deletion request / "
                "soft-delete (or a tenant's soft-delete) before the "
                "privacy-erasure sweep purges the user's/tenant's "
                "object-store bytes and crypto-shreds the tenant "
                "encryption key. Rows are never deleted (ISO 27001).",
)
# GSEC R-120 — tenant-level DSAR two-step erasure: minimum wait after the
# request step before the token confirmation is accepted. Fail-closed in
# the other direction: a legal hold anywhere in the tenant (or platform-
# wide) refuses the erasure with 409 until released.
register(
    "privacy.tenant_erasure_confirm_delay_minutes",
    int,
    15,
    group="privacy",
    description="GSEC R-120 — minutes to wait after a tenant-level "
                "erasure request (POST /tenant/dsar/erasure) before the "
                "token confirmation (POST /tenant/dsar/erasure/{id}/"
                "confirm) is accepted. Zero confirms immediately.",
)


# GSEC R-100 — wizard git-clone bounds. Every `git clone` the wizard emits
# (describe-synthesize dockerfile_extras, Containerizer repo clone, build
# DAG) is rewritten to a shallow partial clone with LFS smudge skipped.
# These two DB settings bound the clone/build step itself so an upstream
# repo can't hang a build or exhaust build-container disk.
register(
    "wizard.clone_timeout_s",
    int,
    900,
    group="wizard",
    description="GSEC R-100 — ceiling in seconds for the wizard's "
                "repo-clone step in the build container. Passed to the "
                "generate stage as CLONE_TIMEOUT_S (the Go worker "
                "follow-up enforces it as a subprocess timeout); until "
                "that lands the value is advisory and the shallow "
                "clone + size cap below are the hard bounds.",
)
register(
    "wizard.clone_max_mb",
    int,
    2048,
    group="wizard",
    description="GSEC R-100 — maximum working-tree size in MiB a wizard "
                "repo clone may occupy in the build container before the "
                "clone is rejected (2 GB default; covers the largest "
                "legitimate tool repos). Passed as CLONE_MAX_MB to the "
                "generate stage; enforced by the Containerizer's "
                "clone_repo_tool which measures the tree after clone.",
)


# Workflow-wizard SSE (e2e-full 10-03 row 3) — per-LLM-call ceiling for one
# wizard turn so a hung model can't hold the stream (and the composer) open.
register(
    "wizard.llm_call_timeout_seconds",
    float,
    45.0,
    group="wizard",
    description="Maximum seconds a single Workflow Wizard LLM call (intent, "
                "builder, critic) may take before it is abandoned and the "
                "turn moves on to its terminal `final` event. Caps the "
                "provider LLM_TIMEOUT for wizard turns only.",
)
register(
    "privacy.erasure_job_enabled",
    bool,
    True,
    group="privacy",
    description="GSEC R-015/R-016 — enable the daily privacy-erasure sweep. "
                "On by default: it irreversibly deletes object-store bytes "
                "and destroys tenant encryption keys for users/tenants "
                "soft-deleted longer than privacy.erasure_grace_days.",
)
# GY.V8 — biometric (BIPA 740 ILCS 14) scheduled-destruction sweep. Daily,
# it soft-deletes biometric-labelled outputs past their consent's retention
# deadline (stated period, or 3 years from last interaction, whichever
# first) via the existing erasure path, skipping legal holds. Off by default
# (byte destruction is irreversible).
register(
    "privacy.biometric_destruction_job_enabled",
    bool,
    False,
    group="privacy",
    description="GY.V8 (BIPA) — enable the daily scheduled-destruction "
                "sweep for biometric-labelled outputs. Soft-deletes "
                "biometric outputs past their consent retention deadline "
                "(or 3 years from last interaction), skipping legal holds. "
                "Off by default: it queues erasure of stored bytes.",
)

def _validate_redaction_profile(value: str) -> None:
    # Lazy import: utils.pii_redaction reads this registry through
    # get_setting(), so importing it at platform_settings import time would be
    # a cycle.
    from utils import pii_redaction
    if value not in pii_redaction.PROFILES:
        raise ValueError(
            f"privacy.redaction_profile must be one of "
            f"{list(pii_redaction.PROFILES)}, got {value!r}")


# GY.V4a (part of GSEC R-086) — PII/PHI redaction profiles for log lines and
# LLM-bound text. utils.secret_redaction has always covered SECRETS only; this
# setting turns on the PII/PHI detectors in utils/pii_redaction.py on top of
# them. Default "off" keeps today's behaviour exactly as shipped — the owner
# flips it per install (or per the profile GY.C0 recommends for the declared
# environment; see utils.pii_redaction.ENVIRONMENT_PROFILE_MAP, which is
# advisory and never writes this key).
#
# Wired surfaces in this task: the API log redaction filter
# (utils.secret_redaction.SecretRedactionFilter / utils.log_config) and the
# central LLM choke point utils.llm_client.call_wizard_llm. NodeRun
# inputs/outputs, task log tails and the support-chat LLM path are follow-up
# (V4b).
register(
    "privacy.redaction_profile",
    str,
    "off",
    validator=_validate_redaction_profile,
    group="privacy",
    description="GY.V4a — PII/PHI redaction profile applied to log lines and "
                "LLM-bound prompts on top of the existing secret redaction. "
                "'off' (default, unchanged behaviour), 'standard' (email, "
                "phone, SSN, payment card, IBAN), 'hipaa' (standard + labelled "
                "MRN + labelled DOB), 'strict' (hipaa + IPv4). Matches become "
                "typed tokens like [REDACTED:EMAIL]. Read through a short-TTL "
                "cache; a settings-read failure falls back to 'off'.",
)


def _validate_nonneg_int(value: int) -> None:
    if value < 0:
        raise ValueError(f"{value!r} must be >= 0")


# Q17 (owner decision, 2026-10-03) — sales-lead PII retention.
# Read by services/scheduler.py::leads_retention_tick (daily). A lead's
# personal fields (name, email, free-text message) are scrubbed to NULL after
# N months open-with-no-activity, or after M months once CLOSED. A 0 value
# disables scrubbing for that branch (never). The row itself always stays
# (ISO 27001, no hard deletes) — only the PII columns are erased. Legal hold
# (services/legal_hold.py) always wins: a held lead/tenant is never scrubbed.
# The job is ON by default (leads.retention_job_enabled defaults to True);
# the owner may set it to False to stop the daily scrub.
register(
    "leads.retention_open_inactive_months",
    int,
    12,
    group="privacy",
    validator=_validate_nonneg_int,
    description="Q17 — months an OPEN sales lead may sit with no activity "
                "(no status change) before its PII (name/email/message) is "
                "scrubbed. 0 = never scrub open leads. A converted lead "
                "(already a customer) is always exempt.",
)
register(
    "leads.retention_closed_months",
    int,
    24,
    group="privacy",
    validator=_validate_nonneg_int,
    description="Q17 — months after a sales lead is CLOSED before its PII "
                "(name/email/message) is scrubbed. 0 = never scrub closed "
                "leads.",
)
register(
    "leads.retention_job_enabled",
    bool,
    True,
    group="privacy",
    description="Q17 — enable the daily sales-lead PII retention scrub. ON "
                "by default. When on, each daily tick scrubs eligible leads' "
                "personal fields (name/email/message -> NULL) and stamps "
                "pii_scrubbed_at. The lead row itself is kept for sales "
                "stats. A legal hold always prevents scrubbing (fail closed).",
)
# GSEC FX.39 (owner decision Q21=A, 2026-10-05) — billing-record retention.
# Read by services/scheduler.py::billing_retention_tick (nightly). Billing
# records (invoices, payments, the tax figures on them) are kept for the
# accounting obligation; once a record is older than this window its PERSONAL
# fields (name, email, address, tax id, contact details, free-text PII) are
# irreversibly crypto-shredded while the row and every financial figure stay
# (anonymised). No hard delete — ISO 27001. 0 disables the shred (records kept
# with their personal fields forever). A legal hold (services/legal_hold.py,
# GY.C15) always prevents shredding, and a hold-lookup error fails closed.
register(
    "billing.record_retention_years",
    int,
    10,
    group="privacy",
    validator=_validate_nonneg_int,
    description="FX.39 (Q21=A) — how long billing records (invoice / payment / "
                "tax rows) are kept before the PERSONAL fields on them are "
                "irreversibly crypto-shredded. The row and its financial "
                "figures always stay, anonymised (ISO 27001: no hard delete). "
                "0 = never shred. A legal hold always prevents it.",
)

# GSEC FX.96 (owner decision Q82=A, 2026-10-05) — log hygiene. Read by
# utils/secret_redaction.py::SecretRedactionFilter at filter-setup time.
register(
    "logging.hash_emails",
    bool,
    True,
    group="privacy",
    description="FX.96 (Q82=A) — hash e-mail addresses in MOD system logs "
                "(sha256 digest prefix). ON by default.",
)
# GSEC FX.16 R-024 (SEC-14-003) — per-tenant LLM data-routing control.
#
# All tenant LLM traffic (assistant chat, support chat, and the tenant log/
# error text the assistant's fetch_logs tool feeds the model) resolves
# through ONE platform-global provider binding (the "assistant" tool slot in
# Settings → AI Configuration). With no opt-in, a tenant whose data is being
# processed by an EXTERNAL (non-MOD-hosted) model endpoint has no way to stop
# it, and the platform cannot distinguish "we sent this to the cloud" from
# "we kept it local".
#
# When True (the default) a tenant whose AI binding resolves to an EXTERNAL
# provider endpoint may only receive LLM traffic if that tenant has explicitly
# opted in — stored as a tenant-scope override of THIS key (see
# `resolve_llm_data_routing` in `routers.ai_config`). Tenants with no override
# inherit the platform value; an external binding for a not-opted-in tenant is
# REFUSED with a clear error naming this setting — no silent fallback to a
# different model. Local / MOD-hosted bindings (managed Ollama, the vLLM
# aliases, PersistentStacks on the mesh/tailnet, localhost) are NEVER external
# and are always allowed regardless of the flag.
#
# A tenant opts in by setting `ai.external_llm_requires_tenant_opt_in` to
# False AT TENANT SCOPE (the platform settings write path already supports
# tenant-scoped rows). The platform-wide value above is the policy default,
# not an opt-in.
register(
    "ai.external_llm_requires_tenant_opt_in",
    bool,
    True,
    group="ai",
    description="GSEC FX.16 R-024 — when True, a tenant whose AI provider "
                "binding resolves to an EXTERNAL (non-MOD-hosted) endpoint "
                "may not receive LLM traffic unless that tenant has opted in "
                "by setting this key to False at tenant scope. Local / "
                "MOD-hosted bindings are never external and are always "
                "allowed. Default True (gate on). Read by "
                "routers.ai_config.resolve_llm_data_routing.",
)
# GSEC FX.16 R-022 — human-confirmation gate for the Workflow Wizard agent
# loop. When True (default), the agentic loop (services/wizard_agent.py) must
# stop BEFORE it dispatches any LLM-generated candidate graph to a worker for
# a sandbox test run: it returns result=NEEDS_CONFIRMATION carrying the
# proposed graph, and execution happens only on the explicit, user-initiated
# POST /support/workflow-wizard/test-run call. Generation, static graph
# validation and dry analysis (graph critique) stay unattended.
register(
    "wizard.require_human_confirm_before_exec",
    bool,
    True,
    group="wizard",
    description="GSEC R-022 — when True, the wizard agent loop refuses to "
                "auto-execute LLM-generated graphs and instead stops with a "
                "NEEDS_CONFIRMATION result; the user must explicitly trigger "
                "the test run (POST /support/workflow-wizard/test-run). "
                "Generation and static validation are unaffected.",
)
# GSEC FX.16 R-023 — build-credential isolation. An LLM-generated
# Dockerfile/entrypoint (sourced from untrusted repo content) is built by the
# build-DAG docker-build stages; before R-023 every build-task stage carried
# the tenant Harbor push robot AND the platform Harbor creds in its env —
# a credential-exfiltration surface during `docker build`. When True
# (default), submit.py attaches per-stage secret_refs: docker-build stages
# get the platform-Harbor PULL creds only, and only the push/pull stages get
# a short-lived (1-day), repo-scoped Harbor robot minted per build (push +
# pull on that repository only). When False the legacy behaviour (full
# BUILD_TASK_SECRET_REFS on every stage) is restored.
register(
    "node_builder.isolate_build_credentials",
    bool,
    True,
    group="node_builder",
    description="GSEC FX.16 R-023 — when True, build-DAG stages receive "
                "isolated, stage-scoped registry credentials: docker-build "
                "stages get platform-Harbor pull creds only and push stages "
                "get a short-lived, repo-scoped Harbor robot instead of the "
                "tenant's durable push robot. When False, the legacy "
                "all-creds-on-every-stage behaviour is restored.",
)
# GSEC FX.17 R-069 — native-backend tenant-code gate (worker tunables,
# pushed via heartbeat; see routers/worker_management._WORKER_TUNABLE_KEYS).
register(
    "worker.native_allow_unsandboxed_tenant_code",
    bool,
    False,
    group="worker",
    description="GSEC FX.17 R-069 — when False (default), a worker running "
                "the native (no-container) backend refuses tenant tasks "
                "outright; platform/system tasks still run. Set True only on "
                "single-tenant / trusted on-prem workers that cannot run "
                "containers.",
)
register(
    "worker.native_drop_uid",
    int,
    0,
    group="worker",
    description="GSEC FX.17 R-069 — uid that native-backend task processes "
                "drop to when the worker runs as root (Linux only). 0 = no "
                "drop.",
)
# GSEC FX.19 R-042 — foreign-egress kill switches (SEC-20-003).
#
# `docs/security-audit/EGRESS_INVENTORY.md` lists every path that can send
# data out of an install. The API-side ones that carry tenant content
# (NIM node calls, HuggingFace weight pulls, external-LLM calls,
# tenant-supplied webhooks/Slack, wizard internet lookups, license
# phone-home) each get a platform-wide boolean, ALL defaulting to True so a
# deploy changes nothing. `egress.sealed_mode` is the GY.C8 master switch:
# when True it forces every category to refused (see
# `utils/egress_policy.py::egress_allowed`, which checks it first). Frontend
# CDN rows are a separate task and are not covered here.
register(
    "egress.sealed_mode",
    bool,
    False,
    group="egress",
    description="GSEC FX.19 R-042 — master egress kill switch. When True, "
                "every per-category egress switch is treated as disabled "
                "(NIM, model downloads, external LLM, webhooks, wizard "
                "internet, license phone-home). Zero outbound calls from the "
                "API for the gated categories. Default False.",
)
register(
    "egress.nim_enabled",
    bool,
    True,
    group="egress",
    description="GSEC FX.19 R-042 — allow outbound calls to NIM catalog/"
                "endpoint servers (services/nim_router_service.py). When "
                "False, NIM catalog fetches are refused and the local DB "
                "catalog is served instead. Overridden by egress.sealed_mode.",
)
register(
    "egress.model_download_enabled",
    bool,
    True,
    group="egress",
    description="GSEC FX.19 R-042 — allow outbound HuggingFace downloads and "
                "size probes (utils/embedding_model_store.py, "
                "routers/ai_config.py weight probe). When False, only "
                "locally-cached / MinIO-mirrored weights are used and a "
                "missing model is an explicit error. Overridden by "
                "egress.sealed_mode.",
)
register(
    "egress.external_llm_enabled",
    bool,
    True,
    group="egress",
    description="GSEC FX.19 R-042 — allow LLM traffic to EXTERNAL (non-"
                "MOD-hosted) provider bindings, as classified by "
                "routers/ai_config.llm_binding_is_external. When False, "
                "external bindings are refused (a clear error naming this "
                "setting); local / MOD-hosted bindings are always allowed. "
                "Overridden by egress.sealed_mode.",
)
register(
    "egress.webhooks_enabled",
    bool,
    True,
    group="egress",
    description="GSEC FX.19 R-042 — allow outbound tenant-supplied webhook "
                "calls: workflow triggers (poll/HTTP-poll), HTTP-trigger "
                "firing + alert callbacks, run-completion webhooks, and "
                "Slack event notifications. When False, sends are skipped "
                "with an audit-safe log line (the run itself is not failed). "
                "Overridden by egress.sealed_mode.",
)
register(
    "egress.wizard_internet_enabled",
    bool,
    True,
    group="egress",
    description="GSEC FX.19 R-042 — allow the Node Wizard's internet "
                "lookups (PyPI package metadata, GitHub READMEs, web query "
                "expansion in node_builder/install_docs.py and "
                "node_builder/web_expand.py). When False, lookups return "
                "empty results and the wizard proceeds without them. "
                "Overridden by egress.sealed_mode.",
)
register(
    "egress.license_phonehome_enabled",
    bool,
    True,
    group="egress",
    description="GSEC FX.19 R-042 — allow the license client's server "
                "validation call (utils/license_client.py "
                "validate_license_online). When False, server validation is "
                "skipped and the existing offline/cached path is used "
                "(a valid license must NOT fail — this mirrors the "
                "network-unreachable behaviour). Overridden by "
                "egress.sealed_mode.",
)
# GY.C9 remainder — update-check + telemetry kill switches. Same family,
# same fail-safe defaults (True = current behaviour; a deploy changes
# nothing until an admin turns a switch off).
register(
    "egress.update_check_enabled",
    bool,
    True,
    group="egress",
    description="GY.C9 — allow version/update-check egress: the worker "
                "heartbeat's `update_available` advertisement (routers/"
                "worker_management.py, fed by utils/worker_releases) and "
                "the admin currency-rate fetch from exchangerate-api.com "
                "(services/admin_plans_currencies_service.py). When False, "
                "workers are never advertised a newer version (no drain/"
                "self-update trigger) and the currency-rate refresh raises "
                "instead of calling out. Overridden by egress.sealed_mode.",
)
register(
    "egress.telemetry_enabled",
    bool,
    True,
    group="egress",
    description="GY.C9 — allow usage-telemetry ingestion: the login-page "
                "RUM first-paint beacon (routers/sso_rum.py -> Prometheus "
                "histogram login_page_first_paint_ms). When False the "
                "beacon endpoint acknowledges the POST but records nothing. "
                "Overridden by egress.sealed_mode.",
)
# GSEC R-011 — trusted-proxy CIDRs for X-Forwarded-For / X-Real-IP handling.
# Before R-011 every client-IP consumer read XFF from ANY peer (and nginx
# trusted all of RFC1918), so a public client could spoof its identity for
# rate limits, audit rows, SCIM IP allowlists and run-surface caps. The
# shared resolver (utils/client_ip.py) honours forwarded headers ONLY when
# the direct TCP peer falls inside one of these CIDRs. Default = loopback
# (in-container calls) + the docker bridge network (172.16.0.0/12, where
# the nginx reverse proxy lives). An operator with an external edge (e.g.
# Cloudflare) adds that edge's ranges here so public clients keep their
# real, non-spoofable address.
register(
    "net.trusted_proxy_cidrs",
    str,
    # CE ships no reverse proxy (the API port is published directly), so the
    # CE default trusts loopback only; paid keeps the docker-bridge range
    # where its nginx edge sits. Overridable in Platform Settings either way.
    _ce_unlimited("127.0.0.1/32,::1/128,172.16.0.0/12",
                  unlimited="127.0.0.1/32,::1/128"),
    group="networking",
    description="GSEC R-011 — comma-separated CIDRs of direct TCP peers whose "
                "X-Forwarded-For / X-Real-IP headers the API honours "
                "(utils/client_ip.py trusted_client_ip). Default: loopback + "
                "docker bridge (nginx). Outside these, forwarded headers are "
                "ignored and the peer address is the client IP. Read from "
                "rate_limiter, audit, SCIM, run-surface and metrics-gate "
                "client-IP resolution.",
)
register(
    "labels.block_external_llm",
    bool,
    True,
    group="labels",
    description="GY.C7 — block restricted-LABELLED tenant content (data "
                "classification restricted / CUI / EAR / ITAR, per the "
                "cogs.data_classification taxonomy) from EXTERNAL "
                "(non-local, non-managed) LLM providers. When True (default) "
                "a request whose referenced asset(s) carry one of those "
                "labels is refused before the provider is called, even if "
                "the tenant opted into external LLM (ai.external_llm_requires"
                "_tenant_opt_in). Local / MOD-hosted bindings are always "
                "allowed. Read fail-safe: a settings-store read error BLOCKS "
                "(utils/label_policy.py external_llm_blocked_for, wired into "
                "routers/ai_config.resolve_llm_data_routing).",
)
# GY.V2 R-133 — opt-in download/export FILENAME marking. When True, every
# download/export delivery of a file carrying an export-controlled label
# (CUI/EAR/ITAR) gets its Content-Disposition filename prefixed with
# "[CUI] " / "[EAR] " / "[ITAR] ". The X-MOD-Classification / X-MOD-Handling
# response headers (services/data_classification.py
# delivery_classification_header / handling_caveat) are always sent on
# labelled deliveries and do not depend on this flag. Default False keeps
# every existing filename byte-identical.
register(
    "labels.mark_download_filenames",
    bool,
    False,
    group="labels",
    description="GY.V2 R-133 — opt-in FILENAME marking on downloads and "
                "exports. When True, the Content-Disposition filename of a "
                "labelled (CUI/EAR/ITAR) delivery is prefixed '[CUI] ' / "
                "'[EAR] ' / '[ITAR] ' so a saved file visibly carries its "
                "marking. Internal tiers are never prefixed. Default False "
                "(filenames stay byte-identical); the response-header "
                "marking is independent of this flag. Applied by "
                "services/data_classification.marked_download_filename on "
                "the single-file, stream, zip, folder-zip, multi-zip and "
                "share-link delivery paths.",
)
# GY.C7 — external-egress block (the node-dispatch side of label handling).
# A run whose input cogs carry a classification in this list must NOT run a
# node that is classified EXTERNAL (a node that declares internet egress via
# allowed_egress — external LLM API, HF download, web fetch). Enforced at the
# dispatch choke point (routers/worker_tasks.enqueue_task_internal) by
# utils/label_policy.block_external_egress_for: the node's dispatch is refused
# before any task row is written, the node + run fail with a clear reason, and
# a LABEL_EXTERNAL_EGRESS_BLOCKED audit event is written. Default = the
# handling/export labels; unlabelled (public/internal/confidential) runs never
# match and see no behaviour change. An admin may narrow/extend the list.
register(
    "labels.no_external_egress_labels",
    list,
    ["cui", "itar", "ear"],
    group="labels",
    description="GY.C7 — data-classification labels that forbid EXTERNAL "
                "(internet-egress) node dispatch. A workflow node that "
                "declares internet egress (allowed_egress with kind "
                "'internet' — external LLM API / HF download / web fetch) "
                "is refused at dispatch when any of the run's input cogs "
                "carries one of these labels. Tailnet egress (MOD's own "
                "mesh) is NOT internet egress and is unaffected. Default "
                "['cui','itar','ear'] (the handling/export tiers of the "
                "cogs.data_classification taxonomy — 'pii' and "
                "'pre_release' are NOT stored classification levels, so "
                "they are not in the default); unlabelled content is never "
                "matched so the default is a no-op for ordinary runs. "
                "Enforced by utils/label_policy.no_external_egress_label_for "
                "wired into routers/worker_tasks.enqueue_task_internal.",
)
# GY.C7 — worker pool / region policy. Maps a data-classification label to
# the list of worker pool/region names that may process it. Values match a
# worker's region tag (hardware_specs['hardware_tags'] 'region:<zone>') or
# its worker_name. {} (default) = no restriction (every worker eligible).
# Enforced at claim time (routers/worker_tasks claim guard,
# label_worker_pool_guard): a labelled task is only claimable by a worker
# whose pool/region matches; when a label is restricted to pools that do not
# exist the task WAITS (fail-wait, never fail-route) — it is never routed to
# a non-matching worker. Read via
# utils/label_policy.allowed_worker_pools_for.
register(
    "labels.allowed_worker_pools",
    dict,
    {},
    group="labels",
    description="GY.C7 — per-label worker pool/region restriction. Maps a "
                "data-classification label to a list of worker pool/region "
                "names (a worker's 'region:<zone>' hardware tag, or its "
                "worker_name) that are eligible to process it. {} (default) "
                "means no restriction — every worker can claim any labelled "
                "task. When a label IS restricted, a labelled task is "
                "withheld from every non-matching worker at claim time "
                "(fail-wait: if no matching worker exists it simply waits). "
                "Enforced by utils/label_policy.allowed_worker_pools_for "
                "wired into the routers/worker_tasks claim guard.",
)


# GSEC FX.13 R-059 — validator for gsec.transport.internal_tls_enforce.
_INTERNAL_TLS_MODE_VALUES = ("off", "report", "require")


def _validate_internal_tls_mode(value: str) -> None:
    if value not in _INTERNAL_TLS_MODE_VALUES:
        raise ValueError(
            f"gsec.transport.internal_tls_enforce must be one of "
            f"{sorted(_INTERNAL_TLS_MODE_VALUES)}, got {value!r}"
        )


# GSEC FX.13 R-059 — internal-hop TLS enforcement gate. off (default) =
# today's plaintext behaviour, byte-identical; report = plain-http internal
# hops are logged once per host; require = the internal CA bundle
# (MOD_INTERNAL_CA_BUNDLE) must be present and plain http:// internal URLs
# are refused (utils/internal_tls.py check_internal_url / internal_verify_arg).
# GY.C14 — RFC 9116 security.txt source values. The public unversioned
# surface `GET /.well-known/security.txt` (routers/security_txt.py) renders
# these instead of a static file, so a disclosure contact change is a
# settings write (admin UI / PATCH /v1/platform/settings/{key}) with no
# deploy. security.txt_expires_days sets the RFC-required Expires:
# timestamp (now + N days, re-derived per response).
register(
    "security.contact",
    str,
    "mailto:admin@example.com",
    group="security",
    description="RFC 9116 security.txt Contact: value (URL or mailto:).",
)
register(
    "security.policy_url",
    str,
    "",
    group="security",
    description="Optional RFC 9116 security.txt Policy: URL (empty = omitted).",
)
register(
    "security.txt_expires_days",
    int,
    365,
    group="security",
    description="Days from now for the security.txt Expires: RFC 3339 "
                "timestamp (re-derived on each response).",
)


register(
    "gsec.transport.internal_tls_enforce",
    str,
    "off",
    validator=_validate_internal_tls_mode,
    group="gsec",
    description="GSEC R-059 — enforcement posture for TLS on internal "
                "service hops (Qdrant, vLLM/LLM, ...). off = unchanged "
                "(plaintext allowed, no CA needed); report = a plain "
                "http:// internal hop logs once per host (soak telemetry) "
                "and the internal CA bundle (MOD_INTERNAL_CA_BUNDLE) is "
                "used when present; require = CA bundle mandatory (missing "
                "bundle raises) and plain http:// internal URLs are "
                "refused. Read by utils/internal_tls.py for the "
                "requests/httpx verify= value on wired internal hops.",
)


# FX.124 part 3 (owner Q111=A) — validator for tls.min_version.
_TLS_MIN_VERSION_VALUES = ("1.2", "1.3")


def _validate_tls_min_version(value: str) -> None:
    if value not in _TLS_MIN_VERSION_VALUES:
        raise ValueError(
            f"tls.min_version must be one of {list(_TLS_MIN_VERSION_VALUES)}, "
            f"got {value!r}"
        )


# FX.124 part 3 (owner Q111=A) — minimum TLS protocol at the REAL public
# edge (front Caddy + stack-ingress Caddy). "1.2" (default) allows TLS 1.2 +
# 1.3 with the AEAD-only ECDHE cipher set + modern curves; "1.3" allows TLS
# 1.3 ONLY (the gov/DoD compliance posture). Locked to "1.3" by the cmmc and
# fedramp_high_il5 profiles in services/compliance_profiles.py.
#
# Who reads it (no new env vars — the value rides the existing seams):
#   * Front Caddy: services/app_routing.build_full_caddyfile injects the
#     policy into every TLS site on each admin-API /load, so the platform
#     sites (from the rendered base) and every MOD app site carry it with no
#     container restart.
#   * Stack-ingress Caddy: scripts/caddy-entrypoint.sh fetches
#     GET /persistent-stacks/ingress/tls-min-version (token-guarded, same
#     seam as the on-demand-TLS `ask`) at render time and renders the site's
#     tls {} block accordingly — picked up on the next stack deploy/recreate.
register(
    "tls.min_version",
    str,
    "1.2",
    validator=_validate_tls_min_version,
    group="gsec",
    description="FX.124 part 3 (Q111=A) — minimum TLS protocol version for "
                "the public edge (front Caddy platform + app sites and the "
                "stack-ingress Caddy). '1.2' = TLS 1.2+1.3 (AEAD-only "
                "ciphers, x25519mlkem768/x25519/secp384r1/secp256r1 curves); "
                "'1.3' = TLS 1.3 ONLY (gov/DoD posture; locked to this "
                "value by the cmmc + fedramp_high_il5 profiles).",
)


# GY.C12 (launch-completion-megaplan) — continuous conformance engine
# cadence. Read by services/scheduler.py::conformance_run_tick on every
# tick (0 = scheduled runs disabled; on-demand runs via
# POST /platform/conformance/run keep working).
register(
    "conformance.run_interval_minutes",
    int,
    360,
    group="conformance",
    description="GY.C12 — minutes between scheduled continuous-conformance "
                "runs (services/conformance/engine.py run_checks, every "
                "check's pass/fail + drift persisted to the append-only "
                "conformance_check_results table). 0 = scheduled runs "
                "disabled (no restart needed; on-demand runs still work). "
                "Default 360 (hourly).",
)

# GY.C12 remainder — drift notification fan-out. Read by
# services/conformance/engine.py::_fan_out_drift once per scheduled/on-demand
# run that detected drift (pass->fail flips). Master switch for the whole
# fan-out: in-app notifications to every active platform admin (one per run,
# listing the drifted check ids) + the tenant webhook fan-out
# (services/outbound_webhooks.dispatch_event, event `conformance.drift`).
# The CONFORMANCE_DRIFT audit row itself is always written regardless of this
# switch, and it already flows to the SIEM shipper (services/siem_export.py
# ships every permission_audit_logs row past siem.export_checkpoint — no
# second ship here). Webhooks additionally honour egress.sealed_mode /
# egress.webhooks_enabled. Default True.
register(
    "conformance.drift_notify",
    bool,
    True,
    group="conformance",
    description="GY.C12 — fan out ONE drift notification per conformance run "
                "that detected drift (pass->fail flips): in-app bell "
                "notification to every active platform admin listing the "
                "drifted check ids, plus the tenant webhook fan-out "
                "(event `conformance.drift`, gated by egress.sealed_mode / "
                "egress.webhooks_enabled). The CONFORMANCE_DRIFT audit row "
                "is written unconditionally and already flows to the SIEM "
                "shipper. False disables the fan-out channels. Default True.",
)


# Q9 part 1 — cadence (seconds) for the Vault seal-state monitor. Read by
# services/scheduler.py::_vault_seal_check_tick on every heartbeat (the
# job is registered at a 30s heartbeat and no-ops until >= this many
# seconds have elapsed since the last probe, so a value <= 0 disables the
# check without a restart and a change takes effect on the next heartbeat).
register(
    "vault.seal_check_interval_seconds",
    int,
    60,
    group="security",
    description="Q9 — seconds between Vault seal-state probes "
                "(services/vault_seal_monitor.py). On a transition to "
                "sealed it writes one VAULT_SEALED audit event + admin "
                "alert (VAULT_UNSEALED on recovery). 0 disables the check "
                "(no restart needed). Default 60 (1 minute).",
)
# GSEC Q21.4 — per-worker Vault AppRole lifecycle (launch-completion-megaplan).
# 'off' (default) is fully inert: no Vault calls, heartbeat response
# byte-identical to pre-Q21.4. 'report'/'enforce' both run the per-worker
# AppRole lifecycle (ensure at enrollment, revoke at deactivation/removal,
# masked status on the heartbeat `vault` object); enforce is the cutover
# posture Q21.12 flips once every worker runs the per-worker-creds release.
# See the internal design notes
# gsec-q21-least-privilege.md (Q21.4).


def _validate_approle_per_worker_mode(value: str) -> None:
    if value not in ("off", "report", "enforce"):
        raise ValueError(
            f"worker.approle_per_worker_enforced must be one of "
            f"'off'|'report'|'enforce', got {value!r}"
        )


def _validate_creds_refresh_interval(value: int) -> None:
    if not 30 <= value <= 3600:
        raise ValueError(
            f"worker.creds_refresh_interval must be 30-3600 seconds, "
            f"got {value!r}"
        )


def _validate_sts_session_ttl_s(value: int) -> None:
    if not 60 <= value <= 86400:
        raise ValueError(
            f"worker.sts_session_ttl_s must be 60-86400 seconds, "
            f"got {value!r}"
        )


register(
    "worker.approle_per_worker_enforced",
    str,
    "off",
    validator=_validate_approle_per_worker_mode,
    restart_required=False,
    group="worker",
    description="GSEC Q21.4 — per-worker Vault AppRole enforcement mode. "
                "'off' (default) = inert: no AppRole lifecycle, no "
                "heartbeat `vault` object. 'report'/'enforce' = per-worker "
                "AppRoles are ensured at enrollment, revoked at "
                "deactivation/removal, and the masked approle_status is "
                "delivered on the heartbeat `vault` object. Flips to "
                "'enforce' at the Q21.12 cutover.",
)
register(
    "worker.creds_refresh_interval",
    int,
    300,
    validator=_validate_creds_refresh_interval,
    restart_required=False,
    group="worker",
    description="GSEC Q21.2/Q21.4 — seconds between per-task credential "
                "refreshes for the worker's creds-refresher loop. Delivered "
                "via the heartbeat `tunables` map (no worker restart). "
                "Default 300.",
)
def _validate_host_port_give_up_s(value: int) -> None:
    if not 0 <= value <= 86400:
        raise ValueError(
            f"stacks.host_port_conflict_give_up_s must be 0-86400 seconds, "
            f"got {value!r}"
        )


register(
    "stacks.host_port_conflict_give_up_s",
    int,
    600,
    validator=_validate_host_port_give_up_s,
    restart_required=False,
    group="stacks",
    description="How long (seconds) a stack deploy whose fixed host port is "
                "already in use on the worker is retried before it is marked "
                "failed with the port and holder named on the stack. "
                "0 = keep retrying. Default 600.",
)
register(
    "worker.sts_session_ttl_s",
    int,
    7200,
    validator=_validate_sts_session_ttl_s,
    restart_required=False,
    group="worker",
    description="GSEC Q21.2 — TTL (seconds) for per-task STS sessions "
                "minted at /workers/tasks/{id}/credentials for BYO-S3 "
                "connections. Default 7200 (2h).",
)


# GY.V3a (launch-completion-megaplan) — data-residency pin + gate for
# storage. Platform setting declaring the residency zone tenant data may
# live in: "us" or "eu". Empty (default) = OFF, byte-identical behaviour.
# Gates (utils/residency.py): (1) StorageConnection create/update refuses
# a region/endpoint mapping outside the pin or to "unknown" (403 + audit
# row), and (2) utils/storage.resolve_backend fails CLOSED (raises
# ResidencyPinError) for an active BYO connection outside the pin. The
# zone is computed from the EXISTING region/endpoint fields via
# utils/residency.REGION_ZONE_MAP — no column added. Conformance check
# "residency.stores_within_pin" (GY.C12) reports per-store zones.


def _validate_residency_pin(value: str) -> None:
    v = str(value or "").strip().lower()
    if v not in ("", "us", "eu"):
        raise ValueError(
            f"residency.region_pin must be one of 'us'|'eu' or empty "
            f"(off), got {value!r}"
        )


register(
    "residency.region_pin",
    str,
    "",
    validator=_validate_residency_pin,
    group="residency",
    description="GY.V3a — platform data-residency pin for tenant storage "
                "stores ('us' / 'eu'; empty = off, default). When set, "
                "creating/updating a StorageConnection whose region "
                "maps outside the pin (or to unknown) is refused with a "
                "403 + audit row, and the storage resolver fails closed "
                "on active connections outside the pin. Provider region "
                "codes map via utils/residency.REGION_ZONE_MAP. See "
                "utils/residency.py (partially closes GSEC R-089/R-181).",
)


# GY.C9 remainder — per-store residency DECLARATIONS (hardened deployment).
#
# `residency.region_pin` (above) is a POLICY gate: it refuses storage that
# lands outside a zone. The declarations here are REPORTING: an operator
# declares where each long-lived store actually lives (postgres,
# object_storage (the versitygw/MinIO default + BYO StorageConnections),
# valkey, qdrant, backups = the off-box shipping target) so a CUI/CJIS
# customer can see residency in the compliance egress-status report.
#
# Both settings are declaration-based: no cloud API is ever called.
# `residency.required_region` empty (default) = nothing is asserted, the
# GY.C12 conformance check is "manual" (the operator verifies residency by
# their own evidence); once set, the check "residency.required_region_match"
# fails when any store declares a zone outside the required region (or
# "unknown").


def _validate_residency_required_region(value: str) -> None:
    v = str(value or "").strip().lower()
    if v not in ("", "us", "eu"):
        raise ValueError(
            f"residency.required_region must be one of 'us'|'eu' or empty "
            f"(manual), got {value!r}"
        )


def _validate_residency_store_regions(value: dict) -> None:
    """Each value must be 'us' | 'eu' | 'unknown' (or empty = undeclared)."""
    allowed = ("", "us", "eu", "unknown")
    for store, zone in value.items():
        z = str(zone or "").strip().lower()
        if z not in allowed:
            raise ValueError(
                f"residency.store_regions[{store!r}] must be one of "
                f"'us'|'eu'|'unknown'|'' (undeclared), got {zone!r}"
            )


register(
    "residency.required_region",
    str,
    "",
    validator=_validate_residency_required_region,
    group="residency",
    description="GY.C9 — the region the customer requires long-lived data "
                "stores to live in ('us' / 'eu'); empty (default) = no "
                "requirement declared, the conformance check "
                "residency.required_region_match reports 'manual'. When "
                "set, the check fails if any store's declared region "
                "(residency.store_regions) maps outside it or is unknown. "
                "Declaration-based — no cloud API calls.",
)
register(
    "residency.store_regions",
    dict,
    {},
    validator=_validate_residency_store_regions,
    group="residency",
    description="GY.C9 — operator-declared residency region per long-lived "
                "data store: postgres, object_storage (versitygw/MinIO "
                "default + BYO StorageConnections), valkey, qdrant, "
                "backups (off-box shipping target). Values: 'us' | 'eu' | "
                "'unknown' (known but not mappable) | ''/absent (undeclared, "
                "reported as 'undeclared'). Read-only in the compliance "
                "egress-status report. Declaration-based — no cloud API "
                "calls; the operator is the source of truth.",
)


def _validate_watermark_policy(value: str) -> None:
    if value not in ("off", "label_only", "all"):
        raise ValueError(
            f"delivery.watermark_policy must be one of "
            f"['off', 'label_only', 'all'], got {value!r}")


def _validate_watermark_min_label(value: str) -> None:
    """GY.V1b — the minimum label must be a known classification level."""
    from utils.paid_import import optional_paid
    _dc = optional_paid("services.data_classification")
    if _dc is None:
        # 2400LR-ceimpF2 CE carve: services/data_classification.py is denied
        # (manifest :1037), so its LEVELS ranking is absent in CE. The gate that
        # reads this setting is already inert there — services/cogs_assets_cogs_files_service.py
        # ._watermark_label_applies returns False when optional_paid(...) is None —
        # so the stored value is never consulted. Accept it with an explicit log
        # (a bare ImportError here would 500 the PATCH /platform/settings route).
        logger.info("GY.V1b CE carve: compliance.watermark_min_label accepted "
                    "unvalidated (%r) — services.data_classification LEVELS absent",
                    value)
        return
    if value not in _dc.LEVELS:
        raise ValueError(
            f"compliance.watermark_min_label must be one of "
            f"{list(_dc.LEVELS)}, got {value!r}")


def _validate_watermark_unsupported_action(value: str) -> None:
    if value not in ("allow", "block"):
        raise ValueError(
            f"compliance.watermark_unsupported_action must be one of "
            f"['allow', 'block'], got {value!r}")


# GY.V1b (film/TV pre-release leak tracing, TPN) — forensic watermark on
# delivery. We ENABLE customer compliance; we never certify it. Minimal
# viable version: an opaque HMAC-derived ID per (tenant, user, object, ts)
# recorded on the delivery audit row and returned as the X-MOD-Watermark
# response header — NO pixel/byte embedding (that is GY.V1c). 'off'
# (default) is byte-identical to today: no header, no watermark_id.
register(
    "delivery.watermark_policy",
    str,
    "off",
    validator=_validate_watermark_policy,
    group="delivery",
    description="GY.V1b — forensic watermark policy on asset delivery. "
                "'off' (default, unchanged behaviour), 'label_only' "
                "(only cogs carrying the 'pre-release' tag get a watermark "
                "ID), 'all' (every delivery gets one). The ID is recorded "
                "on the delivery audit row (metadata.watermark_id) and "
                "returned as the X-MOD-Watermark response header on the "
                "download route. Pixel embedding (the visible + invisible "
                "layers) is gated separately by the compliance.watermark_* "
                "settings below; see services/watermark.py and "
                "utils/watermark.py.",
)

# GY.V1b (closes GSEC R-112) — the pixel layer on top of the V1b ID: labelled
# assets at or above the minimum label are streamed through the API with a
# tiled visible watermark and the download audit id embedded invisibly in the
# luma channel, so a leaked copy is self-identifying. We ENABLE customer
# compliance (TPN / film-TV pre-release leak tracing); we never certify it.
# Default off = the presign path is byte-identical to today.




# GY.V1d (closes GSEC R-112, remainder) — opt-in/kill-switch gate for the
# INVISIBLE (forensic) layer specifically, on top of (never instead of) the
# existing compliance.watermark_* label gate above. "Which labels require
# it" reuses the SAME compliance.watermark_min_label ranking — there is
# deliberately no second min-label setting, so the visible and invisible
# layers can never disagree about which assets are gated. Default True:
# the invisible layer was already shipped unconditionally (V1b/V1c) and
# two pre-existing tests (test_gy_v1b_watermark.py,
# test_gy_v1c_watermark_paths.py) assert it fires whenever the visible gate
# fires — defaulting this new setting to False would silently regress that
# shipped behaviour. True preserves it while still giving operators a real
# kill-switch (set False to go back to visible-only). When True and a
# gated label's invisible mark cannot be applied (numpy/Pillow runtime
# probe fails, embedding raises, or produces no pixel change), the
# delivery fails CLOSED — see services/watermark_delivery.py
# prepare_watermark_bytes / services/cogs_assets_cogs_files_service.py
# _watermark_download. Decode/verify path:
# POST /v1/platform/compliance/watermark/decode (platform admin only) —
# routers/compliance_watermark_decode.py.
register(
    "labels.invisible_watermark_enabled",
    bool,
    True,
    group="labels",
    description="GY.V1d — when True (default), every delivery gated by "
                "compliance.watermark_downloads_enabled + "
                "compliance.watermark_min_label ALSO gets the invisible "
                "DCT-block forensic mark (not just the visible tiled text), "
                "on all three delivery paths (single-file, share-link, zip/"
                "bulk export). Default True preserves the invisible layer's "
                "pre-V1d always-on behaviour; set False to ship visible-"
                "only. When True, a gated asset whose invisible mark "
                "cannot be applied is REFUSED (fail-closed), never served "
                "unmarked. See utils/watermark.py (embed_invisible / "
                "invisible_watermark_available) and "
                "services/watermark_delivery.py.",
)

# GY.C13b — backup RPO/RTO conformance targets (read by the conformance
# checks in services/conformance/checks.py against the readings written by
# CORE/startup/backup.sh (backup_status.json) and CORE/startup/restore-test.sh
# (restore_test lines in the erasure ledger). Tunables, not flags: the checks
# report measured value + target, so operators can tighten without a code
# change. 0 disables the corresponding check (reports not-applicable).
register(
    "backup.rpo_target_hours",
    int,
    24,
    validator=_validate_non_negative_int,
    group="gsec",
    description="GY.C13b — recovery-point-ago target (hours): the last "
                "verified off-box backup (backup_status.json last_success_utc) "
                "must be at most this many hours old. 0 = check disabled.",
)
register(
    "backup.restore_test_max_age_days",
    int,
    30,
    validator=_validate_non_negative_int,
    group="gsec",
    description="GY.C13b — recovery-action readiness target (days): the last "
                "restore_test line in the erasure ledger must be ok=true and "
                "at most this many days old. 0 = check disabled.",
)
register(
    "backup.rto_target_minutes",
    int,
    240,
    validator=_validate_non_negative_int,
    group="gsec",
    description="GY.C13b — recovery-time-out target (minutes): the last "
                "restore_test line's rto_seconds must be at most this many "
                "minutes. 0 = check disabled.",
)

# GSEC R-148 — encrypted platform backups (GY.C6b). The platform backup
# set (CORE/startup/backup.sh: Postgres app + Keycloak dumps, object
# storage, Vault, Qdrant) is age-encrypted to this recipient — public
# material only, safe in the DB; the private identity key is NEVER on
# the box (the operator holds it, supplies it at restore time via
# restore-test.sh --identity). Read by backup.sh (docker exec against
# this registry — no per-box env for behaviour) and by the
# backup.encrypted conformance check (services/conformance/checks.py).
register(
    "backup.encryption_recipient",
    str,
    "",
    group="gsec",
    description="GSEC R-148 (GY.C6b) — age recipient (public key, e.g. "
                "age1... from `age-keygen`) that platform backup sets are "
                "encrypted to. Empty = backups written unencrypted (WARN "
                "logged, backup.encrypted conformance check fails) unless "
                "backup.encryption_required is explicitly true. Public "
                "material only — never store the private key here.",
)
register(
    "backup.encryption_required",
    bool,
    False,
    group="gsec",
    description="GSEC R-148 — fail-closed posture for backup encryption "
                "(R-141 live-safety pattern): when true and no "
                "backup.encryption_recipient is configured, backup.sh "
                "writes NOTHING and exits non-zero (alert via "
                "backup_status.json last_error). Default false so "
                "existing installs keep producing backups (WARN + the "
                "backup.encrypted conformance check fails) until an "
                "operator configures a recipient; backup.sh additionally "
                "treats installs with NO prior backup sets as required "
                "(fresh installs are fail-closed out of the box).",
)

# GY.C14 — vulnerability age-clock conformance target. Read by the
# conformance check vuln.critical_max_age in
# services/conformance/checks.py against the per-release VEX summary
# (mod.vex-summary.v1, produced by CORE/startup/scripts/generate-vex.sh
# and dropped next to CORE/Metadata by the update flow). A Critical
# without a not_affected/fixed VEX statement older than this fails the
# check; 0 disables it (reports not-applicable).
register(
    "vuln.critical_max_age_days",
    int,
    15,
    validator=_validate_non_negative_int,
    group="gsec",
    description="GY.C14 — max age (days) for an unpatched Critical in the "
                "running release's VEX summary: a Critical without a "
                "not_affected/fixed statement older than this fails the "
                "conformance check (NIST RA-5/SI-2, CMMC RA.L2-3.11.2, "
                "ISO A.8.8). 0 = check disabled.",
)

# GSEC FX.52 (owner Q35=A) — remediation-SLA due dates for the GSEC register
# (docs/security-audit/REMEDIATION_SLA_POLICY.md). The due-date authority is
# tools/register_due_dates.py; the conformance check vuln.remediation_overdue
# (services/conformance/checks.py) reads the register through this path
# setting (repo-relative or absolute; a missing register reports unknown,
# never pass), and services/vuln_overdue_report.py emails the weekly
# overdue list to platform admins (scheduler job vuln_overdue_report).
register(
    "vuln.register_path",
    str,
    "docs/security-audit/REGISTER.md",
    group="gsec",
    description="GSEC FX.52 — path of the GSEC findings register (repo-"
                "relative, or absolute) read by the remediation-SLA "
                "conformance check vuln.remediation_overdue and the "
                "weekly overdue report.",
)
register(
    "vuln.overdue_email_enabled",
    bool,
    True,
    group="gsec",
    description="GSEC FX.52 — send the weekly remediation-SLA overdue "
                "report to platform admins (scheduler job "
                "vuln_overdue_report, Mondays 06:00 UTC). Re-read every "
                "tick, so false disables the job without a restart. "
                "Default true.",
)
# The SLA windows themselves are tunables, not code constants: the due-date
# authority (tools/register_due_dates.py) keeps the policy defaults as a
# fallback, and both consumers (the vuln.remediation_overdue check and the
# weekly overdue report) pass these settings through so an owner can retune
# a window without a code change. 0 = no due date for that severity.
for _fx52_key, _fx52_default in (
    ("vuln.sla_days_critical", 7),
    ("vuln.sla_days_kev_critical", 7),
    ("vuln.sla_days_high", 30),
    ("vuln.sla_days_medium", 90),
    ("vuln.sla_days_low", 0),
):
    _fx52_note = (" 0 for Low means no due date: Low items are fixed in the "
                  "next release."
                  if _fx52_key == "vuln.sla_days_low" else "")
    register(
        _fx52_key,
        int,
        _fx52_default,
        validator=_validate_non_negative_int,
        group="gsec",
        description="GSEC FX.52 — remediation-SLA window (days) from the "
                    "found/confirmed date to the due date for this severity "
                    "in the GSEC register (REMEDIATION_SLA_POLICY.md §2); "
                    "read by the vuln.remediation_overdue check and the "
                    "weekly overdue report. 0 = no due date (disabled)."
                    + _fx52_note,
    )
del _fx52_key, _fx52_default, _fx52_note

# GSEC FX.164 p3 [B] (owner Q151=A) — nightly DAST ingest. The report
# pair (zap-baseline-*.json + zap-api-scan-*.json, written by the
# CORE/startup/dast/ runner into the dast_reports volume) is parsed by
# services/dast_reports and ingested into the vuln register by
# services/dast_ingest.run_ingest_tick; this setting points that tick
# (and the operator) at the report dir.
register(
    "vuln.dast_reports_dir",
    str,
    "/zap/wrk",
    group="gsec",
    description="GSEC FX.164 p3 — directory holding the nightly DAST "
                "report pair (the dast_reports volume; "
                "services/dast_ingest.run_ingest_tick parses the newest "
                "pair and ingests findings into the vuln register). "
                "Empty string = fall back to /zap/wrk.",
)

# GY.C4c — inactive-account auto-disable (NIST 800-53 AC-2(3), ISO 27001
# A.9.2.3). Read by services/inactive_accounts.py, which the scheduler runs
# daily (job `inactive_account_disable`). 0 disables the sweep without a
# restart — the tick re-reads this on every pass. Exempt list is the
# break-glass / service-account carve-out: those addresses are never disabled
# even when they are dormant, so the sweep cannot lock an operator out of the
# box it is meant to protect. Platform admins are NOT exempt by default.
register(
    "auth.inactive_disable_days",
    int,
    0,
    validator=_validate_non_negative_int,
    group="auth",
    description="GY.C4c — AC-2(3): disable accounts with no recorded activity "
                "for more than this many days (last activity = newest "
                "user_sessions.last_active_at, else users.created_at). "
                "0 = sweep disabled (default). Disable is soft: Keycloak "
                "enabled=false + status=DEACTIVATED, never a delete.",
)
register(
    "auth.inactive_disable_exempt_emails",
    list,
    [],
    group="auth",
    description="GY.C4c — addresses the inactive sweep must never disable "
                "(break-glass operators, monitored service accounts). Entries "
                "may be single addresses or comma-separated lists; matched "
                "case-insensitively. Seeded service users are always skipped "
                "regardless of this list.",
)

# GY.C4b — concurrent-session cap (Keycloak user-session limiter).
# Enforced by Keycloak's user-session-limits authenticator (provider id
# "user-session-limits", shipped in Keycloak 26.3.x) installed in
# the realm browser flow (CORE/startup/keycloak-init.sh; installed
# disabled-by-default). services/session_controls.py syncs the authenticator
# config whenever this key (or auth.session_limit_behaviour) changes: the
# authenticator is ENABLED only when the limit is > 0, so 0 = unlimited and
# the default keeps today's behaviour. Service / client-credentials / e2e
# accounts never traverse the browser flow, so they are exempt by
# construction (no home-grown auth — Keycloak owns the limit). The key
# constants are declared at module top (above, near BANNER_SETTING_KEY) so
# the after-commit sync hook can reference them.


def _validate_session_limit_behaviour(value: str) -> None:
    if value not in ("deny_new", "terminate_oldest"):
        raise ValueError(
            f"{value!r} is not a session-limit behaviour — expected one of "
            "'deny_new' (refuse the new session) or 'terminate_oldest' "
            "(end the oldest session to make room)")


register(
    SESSION_LIMIT_SETTING_KEY,
    int,
    0,
    validator=_validate_non_negative_int,
    group="auth",
    description="GY.C4b — max CONCURRENT browser (user) sessions per human "
                "account, enforced by the Keycloak user-session-count "
                "limiter authenticator in the realm browser flow. "
                "0 = unlimited (default; the authenticator stays disabled). "
                "Service accounts, client-credentials clients and e2e "
                "principals do not use the browser flow and are exempt. "
                "Changing this (or auth.session_limit_behaviour) pushes the "
                "new config to Keycloak immediately (after commit; a push "
                "failure is audited, never fails the settings write).",
)
register(
    SESSION_LIMIT_BEHAVIOUR_SETTING_KEY,
    str,
    "deny_new",
    validator=_validate_session_limit_behaviour,
    group="auth",
    description="GY.C4b — what the session limiter does when a user exceeds "
                "auth.session_max_concurrent: 'deny_new' refuses the new "
                "login (default) or 'terminate_oldest' ends the OLDEST "
                "session so the new one can start.",
)

# GY.C4b — re-authentication for privileged actions. dependencies.py
# exposes `require_fresh_auth` (a Depends that inspects the KC access
# token's `auth_time` claim): when the token is older than this many
# seconds, privileged endpoints answer 401 {"error": "reauth_required",
# "reauth_max_age_s": N} instead of proceeding — the Dashboard api.js
# handler catches that error and bounces the browser through
# prompt=login&max_age=N before retrying. 0 = off (default, current
# behaviour). Service/api-key channels never carry a user auth_time and
# are exempt (the dependency only runs on the listed human routes).
# (The key constant is declared at module top, near BANNER_SETTING_KEY.)
register(
    REAUTH_SETTING_KEY,
    int,
    0,
    validator=_validate_non_negative_int,
    group="auth",
    description="GY.C4b — max age (seconds) of a login (token `auth_time` "
                "claim) accepted for PRIVILEGED actions: break-glass, "
                "compliance-profile apply, platform-settings writes, user "
                "role changes and API-key creation. Stale tokens get a 401 "
                "with error='reauth_required' (the Dashboard re-auths "
                "through Keycloak with prompt=login&max_age and retries). "
                "0 = off (default).",
)


# ---------------------------------------------------------------------------
# GSEC R-155 — API-side upload size caps (per route class, DB-tunable)
# ---------------------------------------------------------------------------
# The nginx edge (R-080) already returns 413 above its ceilings, but the API
# is independently reachable (direct :80, mesh vhosts, worker/manager
# subdomains, test clients). middleware/upload_size_cap_middleware.py
# enforces these as the in-process authoritative cap and reads them here so
# operators can tune them without an image rebuild. Defaults mirror the
# R-080 nginx values (CORE/startup/scripts/nginx-entrypoint.sh):
#   default 100m, files/upload 512m, cogs/ephemeral/import 1g,
#   node builds 512m. Per-tenant rows (scope='tenant') override the platform
#   row for that tenant — the standard PSR resolution order.
def _validate_upload_cap_bytes(value: int) -> None:
    if int(value) < 1024:
        raise ValueError(
            f"{value!r} is a sub-1KiB upload cap — refusing to store a cap "
            "that would reject every non-trivial upload")


for _key, _default, _desc in (
    ("uploads.max_bytes_default", 100 * 1024 * 1024,
     "GSEC R-155: API-side cap (bytes) for POST/PUT requests with a body "
     "that match no other upload class. Mirrors the nginx default ceiling "
     "(NGINX_DEFAULT_MAX_BODY_SIZE, 100m)."),
    ("uploads.max_bytes_files", 512 * 1024 * 1024,
     "GSEC R-155: API-side cap (bytes) for POST /v1/files/upload. Mirrors "
     "NGINX_UPLOAD_MAX_BODY_SIZE (512m)."),
    ("uploads.max_bytes_cog", 1024 * 1024 * 1024,
     "GSEC R-155: API-side cap (bytes) for cog asset uploads, resumable "
     "uploads, ephemeral run inputs and workflow zip/text imports. Mirrors "
     "NGINX_COG_UPLOAD_MAX_BODY_SIZE (1g)."),
    ("uploads.max_bytes_node_build", 512 * 1024 * 1024,
     "GSEC R-155: API-side cap (bytes) for node build context + dockerfile/"
     "image pushes. Mirrors NGINX_NODE_BUILD_MAX_BODY_SIZE (512m)."),
):
    register(
        _key,
        int,
        _default,
        group="storage",
        validator=_validate_upload_cap_bytes,
        description=_desc,
    )




# GY.C0 follow-up — passive licence-expiry detection (services.
# license_expiry_check.run_license_expiry_check, driven by the scheduler
# daily). The last-known licence state the tick compares against; a dict
# {"state": "valid"|"grace"|"invalid", "expires_at": str|None}. Default {}
# = never observed (the first observation never fires the drop hook).
register(
    "license.last_known_state",
    dict,
    {},
    group="licensing",
    description="GY.C0 follow-up — last licence state observed by the "
                "passive expiry tick ({state, expires_at}). {} = never "
                "observed.",
)
# How many days before a licence expiry date the tick warns platform admins
# (once per expiry date, deduped). 0 = warning off.
register(
    "license.expiry_warn_days",
    int,
    14,
    group="licensing",
    description="GY.C0 follow-up — warn admins when a valid licence is "
                "this many days (or fewer) from expiry; 0 disables the "
                "warning. Default 14.",
)


# GSEC R-086 (PARTIAL → closed) — NodeRun content bound.
#
# `NodeRun.inputs` / `NodeRun.outputs` (JSONB) and `NodeRun.error_message`
# (Text) take whatever the worker reports back. A node that prints a runaway
# log tail, or a task whose result payload embeds a giant inline blob, used to
# be stored verbatim: one such run pinned the row at tens of MB, and the runs
# list (which loads node_runs) then went quadratic — the same shape as the
# runs-list cartesian stall. There was no cap anywhere on this path.
#
# The cap is a per-string clamp, not a whole-payload reject: a node that logs
# more than the cap must still complete, it just stops contributing unbounded
# text to the row. Identity fields the platform itself depends on
# (content_hash, file paths, cog ids, urls) are short and never near the cap,
# so clamping strings cannot break the output-cache key or fan-out resolution.
# Set it to 0 to disable the clamp (unbounded, i.e. today's pre-fix behaviour)
# — the default is finite on purpose: unbounded storage is the finding.
def _validate_node_run_content_max_chars(value: int) -> None:
    if value < 0:
        raise ValueError(
            "node_run.content_max_chars must be >= 0 (0 = no clamp), got "
            f"{value!r}")


register(
    "node_run.content_max_chars",
    int,
    100_000,
    validator=_validate_node_run_content_max_chars,
    group="gsec",
    description="GSEC R-086 — maximum characters of any single string stored "
                "in NodeRun.inputs / NodeRun.outputs / NodeRun.error_message. "
                "Longer strings are truncated with a '…[truncated N chars]' "
                "marker so the row stays bounded and the run still completes. "
                "0 disables the clamp (unbounded storage, the pre-fix "
                "behaviour). Default 100000.",
)
# ---------------------------------------------------------------------------
# FX.102 (owner Q88=A) — tenant share-link policy (group "share").
#
# Shareable links (models.ShareableLink, cogs shareable-links endpoints) are
# tenant-scoped: every key resolves tenant row > platform row > default
# (services/platform_settings.get_setting with tenant_id), so a tenant admin
# can tighten (or loosen, within the platform row) per tenant. Enforcement
# lives in services/share_link_policy.py: CREATE clamps the expiry to
# share.max_expiry_days, refuses public links when disabled, requires a gate
# password when share.require_password is on; VISIT/DOWNLOAD re-checks
# enabled/expiry/viewer domain/download cap at access time (a link created
# under a permissive policy stays bounded when the policy tightens).
#
# Gov/DoD posture: the cmmc and fedramp_high_il5 compliance profiles
# (services/compliance_profiles.PROFILE_REQUIRED_SETTINGS) require
# share.public_links_enabled == False — applying the profile writes it and
# the profile lock rejects any later write that re-enables it. The access
# path ALSO re-derives the force from the active profile list, so a drifted
# value can never reopen public sharing on a gov/DoD install.
# ---------------------------------------------------------------------------
def _validate_share_max_expiry_days(value: int) -> None:
    if value < 1:
        raise ValueError(
            "share.max_expiry_days must be >= 1 (a public link must carry a "
            f"finite expiry), got {value!r}")


def _validate_share_download_cap(value: int) -> None:
    if value < 0:
        raise ValueError(
            "share.download_cap must be >= 0 (0 = unlimited), got "
            f"{value!r}")


register(
    "share.public_links_enabled",
    bool,
    True,
    group="share",
    description="FX.102 — master switch for public shareable links. When "
                "False, creating a new share link is refused (403) and "
                "every existing link stops serving at visit time (403). "
                "Required False by the cmmc and fedramp_high_il5 "
                "compliance profiles (gov/DoD posture). Default True.",
)
register(
    "share.max_expiry_days",
    int,
    30,
    validator=_validate_share_max_expiry_days,
    group="share",
    description="FX.102 — maximum lifetime of a share link, in days. Links "
                "created with no expiry or with an expiry beyond this "
                "horizon are CLAMPED to now + max_expiry_days at create "
                "time (the clamped value is stored and surfaced). Default "
                "30.",
)
register(
    "share.require_password",
    bool,
    False,
    group="share",
    description="FX.102 — when True, a share link cannot be created "
                "without a gate password (422 otherwise); existing "
                "password-less links still refuse access until re-created "
                "with a password. Default False.",
)
register(
    "share.allowed_viewer_domains",
    list,
    [],
    group="share",
    description="FX.102 — allowed viewer e-mail domains for share links "
                "(e.g. [\"acme.com\"]). Empty list = anyone with the link "
                "(anonymous access allowed). Non-empty = the viewer must "
                "be signed in with an e-mail on one of these domains or "
                "the visit is refused (403). Default [] (open).",
)
register(
    "share.download_cap",
    int,
    0,
    validator=_validate_share_download_cap,
    group="share",
    description="FX.102 — maximum number of downloads PER share link "
                "(0 = unlimited). The counter is "
                "shareable_links.download_count (migration "
                "20261005i_fx102_share_link_policy.py); reaching the cap "
                "refuses further downloads (403) until the link is "
                "re-created or revoked. Visits do not count against the "
                "cap. Default 0 (unlimited).",
)


# ============================================================================
# FX.106 (owner Q93=A) — API rate limits + auto-block settings (item 1+2).
# App-level (works on-prem, no third party). Per-plan limits live on the
# pre-existing `plans.api_rate_limit_per_minute` column (0 = unlimited);
# these knobs add the per-API-key cap, the on/off switch, and the
# auto-block thresholds/durations. Consumed by services/api_rate_limit_guard.py
# (which also owns the setting-key constants). Self-contained block — do not
# reformat the rest of this file.
# ============================================================================
_FX106 = "api.rate_limit"

# CE-aware API rate-limit defaults (owner decision 11-10: Community Edition
# has no quantity caps, so a self-hosted install gets raised numbers). Paid
# keeps its original values. The edition comes from utils.paid_import
# (sentinel-module based; never an env tunable). Read lazily and fail to the
# PAID numbers, so an import problem can never silently loosen paid limits.
_RATE_LIMIT_PAID_DEFAULTS = {
    "per_key": 300,
    "default_rpm": 120,
    "no_subscription": 200,
    "plan_limits": {"free": 300, "starter": 200, "pro": 500, "business": 1000},
}
# FIX-CE-UNLIMITED-B2 (CE has NO API rate limit at
# all. 0 is the guard's "unlimited" sentinel for per_key / default_rpm /
# no_subscription (api_rate_limit_guard + utils.rate_limiter treat 0 as no
# bucket); an EMPTY plan_limits map means no settings-driven plan cap (the
# guard also ignores the plans column on CE). Built through the shared
# _ce_unlimited helper (ce=True: this dict IS the CE side). An operator
# value written to the registry still wins on both editions.
_RATE_LIMIT_CE_DEFAULTS = {
    "per_key": _ce_unlimited(300, ce=True),
    "default_rpm": _ce_unlimited(120, ce=True),
    "no_subscription": _ce_unlimited(200, ce=True),
    "plan_limits": {},
}


def rate_limit_defaults(ce: "bool | None" = None) -> dict:
    """The API rate-limit defaults for this edition (a fresh copy).
    ``ce=None`` asks ``utils.paid_import.is_ce_edition()``; any failure
    returns the paid numbers."""
    if ce is None:
        try:
            from utils.paid_import import is_ce_edition
            ce = bool(is_ce_edition())
        except Exception:  # noqa: BLE001
            ce = False
    src = _RATE_LIMIT_CE_DEFAULTS if ce else _RATE_LIMIT_PAID_DEFAULTS
    out = dict(src)
    out["plan_limits"] = dict(src["plan_limits"])
    return out


def feature_throttle(paid_max: int, ce: "bool | None" = None) -> int:
    """CE only: a feature throttle (assistant chat, wizard test-run, goal
    planner, wizard audit) is x100 — owner decision 11-10 10:50 PM CT
    ("raise them for CE"); the window never moves.

    Same shape as ``rate_limit_defaults``: ``ce=None`` asks
    ``utils.paid_import.is_ce_edition()`` and ANY failure returns the PAID
    number, so an import problem can never silently loosen a paid cap.
    Non-int / bool / non-positive input is returned unchanged.

    On CE the value is multiplied by 100 INCLUDING caller-supplied and
    env-configured values (a per-tenant pin or an operator env tunable
    passed in is raised the same way), so a tightened knob on CE reads
    as tightened x100."""
    if not isinstance(paid_max, int) or isinstance(paid_max, bool) \
            or paid_max <= 0:
        return paid_max
    if ce is None:
        try:
            from utils.paid_import import is_ce_edition
            ce = bool(is_ce_edition())
        except Exception:  # noqa: BLE001
            ce = False
    return paid_max * 100 if ce else paid_max


_FX106_RL = rate_limit_defaults()


def _validate_fx106_rate_limit_int(minimum: int, maximum: int, field: str):
    def _check(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) \
                or not (minimum <= value <= maximum):
            raise ValueError(
                f"{field} must be an integer between {minimum} and "
                f"{maximum} (got {value!r})")
    return _check


def _validate_fx106_exempt_cidrs(value: list) -> None:
    import ipaddress
    for item in value:
        try:
            ipaddress.ip_network(str(item).strip(), strict=False)
        except ValueError:
            raise ValueError(
                "api.rate_limit.auto_block_exempt_cidrs contains a "
                f"non-CIDR entry: {item!r}")


# api.rate_limit.enabled (bool) — master on/off for the app-level guard.
register(
    "api.rate_limit.enabled",
    bool,
    True,
    group=_FX106,
    description="FX.106 — master switch for the app-level API rate-limit "
                "guard (per-plan + per-API-key caps + client-IP auto-block). "
                "Default True. Per-plan caps are the pre-existing "
                "plans.api_rate_limit_per_minute column; this switch and the "
                "other api.rate_limit.* settings layer the per-key cap and "
                "the auto-block on top.",
)
# api.rate_limit.per_key_per_minute (int) — per-API-key cap.
register(
    "api.rate_limit.per_key_per_minute",
    int,
    _FX106_RL["per_key"],
    group=_FX106,
    validator=_validate_fx106_rate_limit_int(_ce_unlimited(1, 0), 1000000,
                                             "api.rate_limit.per_key_per_minute"),
    description="FX.106 — sustained requests/minute allowed PER API key. "
                "A request that arrives with an API key is capped at the "
                "TIGHTER of (its plan's api_rate_limit_per_minute, this "
                "value); 0 = no per-key cap. Default 300/min. Per-tenant "
                "overrides are honoured (the plan's own per-minute cap "
                "still bounds the whole tenant).",
)
# api.rate_limit.auto_block_enabled (bool) — auto-block on/off.
register(
    "api.rate_limit.auto_block_enabled",
    bool,
    True,
    group=_FX106,
    description="FX.106 — auto-block client IPs after repeated failed "
                "logins or repeated rate-limit (429) hits. Default True. "
                "Blocks are time-limited (auto_block_duration_min) and "
                "write an append-only audit row (API_AUTO_BLOCK).",
)
# api.rate_limit.auto_block_after_429s (int) — 429 trip threshold.
register(
    "api.rate_limit.auto_block_after_429s",
    int,
    25,
    group=_FX106,
    validator=_validate_fx106_rate_limit_int(2, 100000,
                                             "api.rate_limit.auto_block_after_429s"),
    description="FX.106 — number of rate-limit (429) responses a client IP "
                "may receive within the window before it is auto-blocked. "
                "Default 25.",
)
# api.rate_limit.auto_block_after_login_failures (int) — login trip.
register(
    "api.rate_limit.auto_block_after_login_failures",
    int,
    20,
    group=_FX106,
    validator=_validate_fx106_rate_limit_int(3, 100000,
                                             "api.rate_limit.auto_block_after_login_failures"),
    description="FX.106 — number of failed logins a client IP may make "
                "within the window before it is auto-blocked. Complements "
                "the per-account Keycloak lockout (auth.lockout_*): this "
                "bounds a single IP trying many accounts. Default 20.",
)
# api.rate_limit.auto_block_window_min (int) — counting window.
register(
    "api.rate_limit.auto_block_window_min",
    int,
    15,
    group=_FX106,
    validator=_validate_fx106_rate_limit_int(1, 1440,
                                             "api.rate_limit.auto_block_window_min"),
    description="FX.106 — the sliding window (minutes) over which failed "
                "logins / 429s are counted toward the auto-block "
                "thresholds. Default 15.",
)
# api.rate_limit.auto_block_duration_min (int) — block duration.
register(
    "api.rate_limit.auto_block_duration_min",
    int,
    60,
    group=_FX106,
    validator=_validate_fx106_rate_limit_int(1, 10080,
                                             "api.rate_limit.auto_block_duration_min"),
    description="FX.106 — how long (minutes) a client IP stays auto-"
                "blocked once a threshold trips. Time-limited by design; a "
                "platform admin can unblock early (audited). Default 60.",
)
# api.rate_limit.auto_block_exempt_cidrs (list) — internal-source
# exemption for the client-IP auto-block.
register(
    "api.rate_limit.auto_block_exempt_cidrs",
    list,
    ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12",
     "192.168.0.0/16", "100.64.0.0/10", "fc00::/7"],
    group=_FX106,
    validator=_validate_fx106_exempt_cidrs,
    description="FX.106 — CIDRs that are NEVER client-IP auto-blocked "
                "(rate-limit 429s still apply to them). Default covers "
                "loopback, RFC1918 private ranges, the tailnet range "
                "100.64.0.0/10 and private IPv6: Go workers (LAN / "
                "tailnet / docker bridge), WorkflowBuilder/Dashboard "
                "server-side calls, switchyard/stacks, the reverse proxy "
                "itself and in-container traffic all sit in these ranges, "
                "so blocking one would be a whole-fleet outage. Gov/"
                "on-prem note: on-prem customers' USERS are usually on "
                "private ranges too, so on-prem effectively relies on "
                "the per-account Keycloak lockout (auth.lockout_*) plus "
                "the public-IP side of this guard; an owner who wants "
                "the auto-block to also cover private client IPs can "
                "narrow this list (e.g. keep only loopback + the "
                "proxy/docker ranges). Trusted-proxy peers "
                "(net.trusted_proxy_cidrs) are exempt regardless of this "
                "list. Default: the seven internal ranges above.",
)


# ---------------------------------------------------------------------------
# FX.110 (owner decision Q97=A) — outside collaborators (group
# "external_collab").
#
# Every external account + grant carries an end date; enforcement lives
# in services/outside_collaborators.py:
#   * default_expiry_days  — the end date stamped on new external
#     invitations/grants when the inviter doesn't pick one;
#   * reminder_days_before — the inviter is reminded N days before the
#     expiry (deduped via users.external_expiry_reminded_at); the daily
#     scheduler job (services/scheduler.py FX.110 block) soft-disables
#     expired access — never a hard delete;
#   * require_mfa          — external users must sign in with MFA or
#     their own company SSO (amr/idp claim check at the auth
#     dependency level);
#   * terms_text           — tenant NDA text; when non-empty the
#     external user must accept before first access (audit row carries
#     the SHA-256 version hash + timestamp);
#   * require_admin_approval — gov/DoD: FORCED True by the cmmc and
#     fedramp_high_il5 compliance profiles (PROFILE_REQUIRED_SETTINGS);
#     applying the profile writes it and the profile lock rejects any
#     later write that clears it; the first-access gate re-derives the
#     force from the ACTIVE profile list so a drifted value can never
#     reopen external access on a gov/DoD install.
#
# No env vars: every tunable here is a tenant-scoped DB platform setting
# (tenant row > platform row > default).
# ---------------------------------------------------------------------------
def _validate_fx110_expiry_days(value: int) -> None:
    if not (1 <= value <= 3650):
        raise ValueError(
            "external_collab.default_expiry_days must be in 1..3650, got "
            f"{value!r}")


def _validate_fx110_reminder_days(value: int) -> None:
    if value < 0:
        raise ValueError(
            "external_collab.reminder_days_before must be >= 0, got "
            f"{value!r}")


register(
    "external_collab.default_expiry_days",
    int,
    90,
    validator=_validate_fx110_expiry_days,
    group="external_collab",
    description="FX.110 — default end date (in days) for new external "
                "invitations and external grants when the inviter does "
                "not pick an explicit expiry. Every external account "
                "and grant is time-bounded; the daily job soft-disables "
                "access at the end date (audit row EXTERNAL_ACCESS_EXPIRED "
                "/ EXTERNAL_USER_EXPIRED; never a hard delete). Default 90.",
)
register(
    "external_collab.reminder_days_before",
    int,
    7,
    validator=_validate_fx110_reminder_days,
    group="external_collab",
    description="FX.110 — the owner/inviter is reminded N days before "
                "an external account's access expires (deduped: each "
                "external account is reminded at most once, via "
                "users.external_expiry_reminded_at). 0 disables the "
                "reminder. Default 7.",
)
register(
    "external_collab.require_mfa",
    bool,
    True,
    group="external_collab",
    description="FX.110 — when True, external users must sign in with "
                "MFA or their own company SSO. Enforced at the token/"
                "auth dependency level by checking the access token's "
                "strong-auth claims (amr otp/webauthn, or a federated "
                "idp claim). Default True.",
)
register(
    "external_collab.terms_text",
    str,
    "",
    group="external_collab",
    description="FX.110 — tenant NDA / collaboration terms text shown "
                "to external users. When non-empty, the external user "
                "must accept it before first access (POST "
                "/public/external-collab/terms/accept); until then every "
                "API call except auth/accept returns 403 "
                "EXTERNAL_TERMS_NOT_ACCEPTED. Acceptance is logged with "
                "a timestamp + SHA-256 version hash of this text. Empty "
                "= gate off.",
)
register(
    "external_collab.require_admin_approval",
    bool,
    False,
    group="external_collab",
    description="FX.110 — when True, external users are OFF unless a "
                "tenant admin approves each one (POST "
                "/external-collab/admin/approve/{user_id}); unapproved "
                "external users get 403 EXTERNAL_PROFILE_APPROVAL_REQUIRED "
                "at first access. Required True by the cmmc and "
                "fedramp_high_il5 compliance profiles (gov/DoD posture); "
                "the first-access gate re-derives the force from the "
                "active profile list. Default False.",
)


# ---------------------------------------------------------------------------
# FX.114 (owner Q101=A) — service accounts + API key lifetime (group
# "auth"). Enforcement lives in services/service_accounts.py:
#
# - auth.api_key_max_lifetime_days: the TENANT max API-key lifetime.
#   create AND rotate refuse an expires_at that is missing, in the
#   past, or beyond this horizon (422). The cmmc and fedramp_high_il5
#   compliance profiles (services/compliance_profiles.
#   PROFILE_REQUIRED_SETTINGS) require ("max", 90) — applying the gov
#   profile writes 90 and the profile lock refuses any later write
#   that raises it above 90 (weakening = 409). Default 365.
#
# - auth.api_key_auto_disable_unused_days: the daily lifecycle job
#   (scheduler id fx114_api_key_lifecycle) DISABLES (never deletes) a
#   live key whose last_used_at (or created_at, if never used) is
#   older than this many days, with an API_KEY_AUTO_DISABLED audit row.
#   0 = the auto-disable half is off. Default 90.
#
# - auth.api_key_rotation_overlap_hours: after POST .../keys/{id}/
#   rotate the OLD key stays valid until now + this window, then the
#   same job disables it. Default 24 (hours; fractional allowed).
# ---------------------------------------------------------------------------
def _validate_fx114_max_lifetime(value: int) -> None:
    if value < 1:
        raise ValueError(
            "auth.api_key_max_lifetime_days must be >= 1 (a finite "
            f"key lifetime), got {value!r}")


def _validate_fx114_auto_disable(value: int) -> None:
    if value < 0:
        raise ValueError(
            "auth.api_key_auto_disable_unused_days must be >= 0 "
            f"(0 = off), got {value!r}")


def _validate_fx114_overlap_hours(value: float) -> None:
    if value < 0:
        raise ValueError(
            "auth.api_key_rotation_overlap_hours must be >= 0 "
            f"(0 = the old key is disabled immediately on rotate), "
            f"got {value!r}")


register(
    "auth.api_key_max_lifetime_days",
    int,
    365,
    validator=_validate_fx114_max_lifetime,
    group="auth",
    description="FX.114 — tenant max API-key lifetime in days: keys "
                "created or rotated must carry an expiry within this "
                "horizon (422 otherwise). Required for every key; "
                "there is no unbounded default. Default 365; the "
                "cmmc and fedramp_high_il5 gov compliance profiles "
                "require <= 90 (profile lock rejects weakening).",
)
register(
    "auth.api_key_auto_disable_unused_days",
    int,
    90,
    validator=_validate_fx114_auto_disable,
    group="auth",
    description="FX.114 — days without use before the daily lifecycle "
                "job auto-DISABLES a live API key (last_used_at or, if "
                "never used, created_at older than this) with an "
                "audit row. Never deleted. 0 = off. Default 90.",
)
register(
    "auth.api_key_rotation_overlap_hours",
    float,
    24.0,
    validator=_validate_fx114_overlap_hours,
    group="auth",
    description="FX.114 — rotation overlap window in hours: after a "
                "rotate the OLD key stays valid until the successor "
                "exists for this long, then the daily job disables it. "
                "0 = disable the old key immediately at rotate time. "
                "Default 24.",
)


# ---------------------------------------------------------------------------
# GSEC FX.92 (owner decision Q78=A) — hosted cancellation lifecycle
# ---------------------------------------------------------------------------

def _validate_fx92_cancellation_export_days(value: Any) -> None:
    """billing.cancellation_export_days: 1..90 (values above 30 apply only
    where the plan allows it; others are capped at 30 at use time,
    services/cancellation_lifecycle.effective_window_days)."""
    v = int(value)
    if v < 1 or v > 90:
        raise ValueError(
            "billing.cancellation_export_days must be 1..90 "
            f"(got {v})"
        )


register(
    "billing.cancellation_export_days",
    int,
    30,
    validator=_validate_fx92_cancellation_export_days,
    group="billing",
    ce_hidden=True,
    description="FX.92 — length (days) of the read-only EXPORT WINDOW a "
                "hosted (SaaS) tenant gets after its subscription is "
                "canceled: files / workflow JSON / run results / the "
                "audit-log export stay downloadable, writes are blocked "
                "(FX.41 gate). Resubscribing during the window restores "
                "normal mode. At window end the tenant is soft-deleted "
                "and the FX.15 byte erasure runs (legal hold blocks it). "
                "Range 1..90; values above 30 apply only where the plan "
                "allows it, otherwise the value is capped at 30 at use time. A "
                "tenant-scoped override wins over the platform value. "
                "Reminder emails fire at day 0, 7, N-7, N-1.",
)



# ---------------------------------------------------------------------------
# FX.104 (owner Q90=A) — support bundle + remote support session
# ---------------------------------------------------------------------------
def _validate_fx104_session_max_hours(value: Any) -> None:
    from services.support_session import max_hours_validator
    max_hours_validator(value)


register(
    "support.remote_session_enabled",
    bool,
    False,
    group="support",
    description="FX.104 (Q90=A) — master switch for the remote support "
                "session feature (Planned: the session broker/agent/ "
                "endpoint are not built yet; only this switch + the "
                "gov/DoD profile guard ship). Default False. When a gov/"
                "DoD profile (cmmc / fedramp_high_il5) is active, "
                "PROFILE_REQUIRED_SETTINGS forces this to False and the "
                "enforcement read (services/support_session."
                "effective_remote_session) re-derives the force from the "
                "active profile list so a drifted DB value can never "
                "re-enable it.",
)
register(
    "support.remote_session_max_hours",
    int,
    4,
    validator=_validate_fx104_session_max_hours,
    group="support",
    description="FX.104 (Q90=A) — maximum duration (whole hours, 1..24) "
                "of a single remote support session. Default 4. Under an "
                "active gov/DoD profile the enforcement read clamps this "
                "to 4 as well.",
)
register(
    "assets.outdated_worker_alert_enabled",
    bool,
    True,
    group="assets",
    description="FX.126 (owner Q113=A) — master switch for the daily "
                "asset-inventory scheduler tick that flags workers whose "
                "reported agent_version is older than the current release "
                "manifest (utils/worker_releases.get_latest_version) and "
                "emits one audit row per outdated worker via the admin "
                "alert path (audit.log_security_event, "
                "AuditAction.ASSET_WORKER_OUTDATED_ALERT). Set False to "
                "freeze the alert (the tick becomes a no-op). The "
                "unknown-device enrollment alert (same audit path) is "
                "always on and independent of this flag.",
)


# ---------------------------------------------------------------------------
# GSEC FX.103 (owner decision Q89=A) — node deprecation / retirement
# ---------------------------------------------------------------------------

def _validate_fx103_deprecation_months(value: Any) -> None:
    """deprecation.min_runnable_months / notice_months: positive ints.

    min_runnable_months is the hard floor between `deprecated_at` and
    `retire_after` — a node stays runnable for pinned workflows at least
    this long after deprecation (validator:
    services/node_deprecation.validate_deprecation). notice_months is the
    UI/notice window that opens at deprecation (part 2 reminder emails
    key on it). Both are policy floors, not hard caps, so 1..120."""
    v = int(value)
    if v < 1 or v > 120:
        raise ValueError("deprecation months must be 1..120 (got "
                         f"{v})")


register(
    "deprecation.min_runnable_months",
    int,
    12,
    validator=_validate_fx103_deprecation_months,
    group="deprecation",
    description="FX.103 — months a deprecated (or retired) node stays "
                "runnable for pinned workflows after deprecated_at. "
                "validate_deprecation rejects any retire_after earlier "
                "than deprecated_at + this many months (default 12). "
                "Retired versions keep running for pinned workflows "
                "until retire_after elapses; the run gate itself is the "
                "part-2 retirement scheduler.",
)
register(
    "deprecation.notice_months",
    int,
    6,
    validator=_validate_fx103_deprecation_months,
    group="deprecation",
    description="FX.103 — length of the deprecation NOTICE window that "
                "opens at deprecated_at: the UI badges the node and the "
                "part-2 notice emails go out during it. Informational "
                "for the validator (the runnability floor is "
                "min_runnable_months); default 6.",
)


# --- FX.112 PART 1 (Q99=A) — account security alerts -----------------------

register(
    "security_alerts.user_emails_enabled",
    bool,
    True,
    group="security",
    description="FX.112 PART 1 — send the user a "
                "security email when an account-security event happens "
                "(MFA added/removed, password changed, API key "
                "created/revoked). The append-only audit row is ALWAYS "
                "written regardless of this setting; it only gates the "
                "email. Default True.",
)

register(
    "security_alerts.new_device_enabled",
    bool,
    True,
    group="security",
    description="FX.112 PART 2 — send the user a "
                "'new sign-in' security alert when a direct (non-IdP) "
                "Keycloak LOGIN event arrives from a device not in the "
                "user's known-device baseline. The first-ever sign-in "
                "seeds the baseline silently (no email); only a later "
                "unseen device alerts. The new-device audit row is still "
                "written regardless. Default True.",
)

register(
    "security_alerts.device_baseline_days",
    int,
    90,
    group="security",
    description="FX.112 PART 2 — how many days a "
                "known-device entry stays in the per-user baseline before "
                "it is pruned. After the window, a device the user last "
                "signed in from within the window is treated as known; "
                "once pruned, signing in from it again re-alerts as a new "
                "device. Default 90.",
)


# ---------------------------------------------------------------------------
# GW.2 fix — staleness of the heartbeat's host-bound-port reading
# ---------------------------------------------------------------------------

def _validate_host_bound_ports_stale_intervals(value: Any) -> None:
    """stack.host_bound_ports_stale_intervals: positive int, 1..100.

    The worker's heartbeat CARRIES the last host-bound-ports reading forward
    when a beat omits the field (absent = unknown, never 'nothing bound'),
    so a reading can outlive the worker that wrote it. Placement ignores the
    reading once it is older than this many heartbeat intervals (default 3,
    ~90s at the worker's 30s cadence). 1..100 keeps it from being set to
    something degenerate."""
    v = int(value)
    if v < 1 or v > 100:
        raise ValueError("host bound-ports staleness must be 1..100 "
                         "heartbeat intervals (got %s)" % (value,))


register(
    "stack.host_bound_ports_stale_intervals",
    int,
    3,
    validator=_validate_host_bound_ports_stale_intervals,
    group="stack",
    description="GW.2 fix — how many heartbeat intervals a stored "
                "host_bound_ports reading (hardware_specs['host_bound_ports'], "
                "capture time hardware_specs['host_bound_ports_at']) may live "
                "before stack placement treats it as absent. The reading is "
                "carried forward when a beat omits the field, so without a "
                "staleness cut a reading could block a port on a worker that "
                "no longer answers the field at all (old binary, provider "
                "gone). Rows stored before the capture stamp existed are "
                "grandfathered (never stale). Default 3 (worker heartbeat "
                "cadence is 30s, so ~90s).",
)


# --- FX.140 PART 2 (Q128=A) — email footer postal address ------------------

register(
    "email.postal_address",
    str,
    "",
    group="email",
    description="FX.140 part 2 (Q128=A) — controller postal address shown "
                "in the footer of NON-service (marketing/product) email, "
                "per CAN-SPAM 16 U.S.C. §7711(a)(1)(iii) (16 CFR Part 30 "
                "email marketing rules: sender identity + physical address "
                "are mandatory). Empty string (default) = no address "
                "configured, so nothing is injected; when non-empty, "
                "utils/email_sender passes it to the React Email template "
                "props as `postal_address` (template rendering itself is "
                "a follow-up). Service/transactional sends never carry it.",
)


# --- GSEC FX.103 PART 2 (owner decision Q89=A) — node retirement --------
# The deprecation VALIDATOR (part 1) enforces the same floors at write
# time via `deprecation.*`; the RETIREMENT SCHEDULER (part 2,
# services/node_retirement.run_retirement_sweep) re-reads the floors at
# run time from these keys. services/node_deprecation.min_runnable_months
# / notice_months prefer the `lifecycle.*` key and fall back to
# `deprecation.*` when only the legacy key has a DB row, so the validator
# and the scheduler can never disagree. Keep the pairs in sync.

def _validate_fx103p2_lifecycle_months(value: Any) -> None:
    """lifecycle.min_runnable_months / notice_months: positive ints, 1..120
    (same policy range as part 1's deprecation.* validator)."""
    v = int(value)
    if v < 1 or v > 120:
        raise ValueError("lifecycle months must be 1..120 (got "
                         f"{v})")


register(
    "lifecycle.min_runnable_months",
    int,
    12,
    validator=_validate_fx103p2_lifecycle_months,
    group="deprecation",
    description="FX.103 part 2 — months a deprecated node stays RUNNABLE "
                "before the retirement scheduler (services/"
                "node_retirement.py) may flip it to 'retired' on its "
                "retirement date: the sweep refuses to retire anything "
                "retired before deprecated_at + this many months "
                "(12 months minimum). Mirrors "
                "part 1's deprecation.min_runnable_months, which the "
                "write-time validator enforces; the shared reader in "
                "services/node_deprecation prefers this key.",
)
register(
    "lifecycle.notice_months",
    int,
    6,
    validator=_validate_fx103p2_lifecycle_months,
    group="deprecation",
    description="FX.103 part 2 — months of NOTICE before retirement: the "
                "daily sweep sends the one-time deprecation notice email "
                "(template 'deprecation-notice' to the tenant admins of "
                "every tenant whose workflows use the node) from "
                "deprecated_at, and refuses to flip a node to retired "
                "before deprecated_at + this many months. Default 6. Mirrors part 1's "
                "deprecation.notice_months; the shared reader in "
                "services/node_deprecation prefers this key.",
)

# --- FX.132 part 3 (owner decision Q120=A) — EU AI Act Art. 8 AUP ack -----

register(
    "ai.prohibited_use_ack",
    bool,
    False,
    group="ai",
    description="FX.132 (Q120=A, EU AI Act Art. 8 prohibited-use) — a tenant "
                "admin's acknowledgement of the AUP (AI Use Policy) clause. "
                "While False (default), starting a workflow run whose graph "
                "contains a node tagged 'prohibited-use:<practice>' (tags "
                "seeded by migrations/20261005r_fx132_prohibited_use_tags.py "
                "on NodeDefinition.custom_metadata['tags']) is refused with "
                "403 naming the node + practice; a tenant-scoped True lets "
                "such runs start. Read by services.ai_prohibited_use."
                "check_run_prohibited_use.",
)

register(
    "ai.visible_label_on_likeness_outputs",
    bool,
    False,
    group="ai",
    description="FX.132 part 3 (Q120=A, EU AI Act Art. 50 'AI-generated' "
                "label option) — when True at tenant scope, outputs of nodes "
                "tagged 'likeness' or 'prohibited-use:*' get the visible "
                "'AI-generated' label. Default False (no label). Decision "
                "helper services.ai_prohibited_use.should_label; wiring the "
                "actual image/video writers is FX.97 C2PA follow-up.",
)


# --- FX.90 part 1 (owner decision Q76=A) — upload malware scanning ---------
# API-side ClamAV (clamd INSTREAM over TCP) scanning of tenant file uploads.
# The ClamAV container + offline signature mirror are a separate workstream;
# these keys drive services/malware_scan.py + the hook in routers/files.py.











# --- Assistant tenant mode ----------

ASSISTANT_MODES = ("on", "read_only", "off")


def _validate_assistant_mode(value: str) -> None:
    if value not in ASSISTANT_MODES:
        raise ValueError(
            f"assistant.mode must be one of {ASSISTANT_MODES}, got {value!r}"
        )


register(
    "assistant.mode",
    str,
    "on",
    validator=_validate_assistant_mode,
    group="ai",
    description="Per-tenant assistant mode. 'on' "
                "(default) allows read and write tools. 'read_only' refuses "
                "any non-read tool (only lookup_site_map / retrieve_docs / "
                "retrieve_company_knowledge / fetch_logs run). 'off' refuses "
                "every tool call. Read by services.assistant_confirm."
                "check_tool_call, which gates the assistant tool-dispatch "
                "point in services/assistant_orchestrator.py.",
)

# --- FX.98 part 1 (owner decision Q84=A) — likeness consent register ----

register(
    "likeness.consent_expiry_reminder_days",
    int,
    14,
    group="compliance",
    description="FX.98 (Q84=A) — window (days before expires_at) in which "
                "a likeness consent shows up in the tenant-admin 'expiring "
                "soon' list (GET /v1/tenant/likeness-consents/expiring, "
                "services/likeness_consent.list_expiring). Default 14. "
                "Reminder DELIVERY (email/notification push to the tenant "
                "admins) is Planned — this part only computes and lists "
                "the window.",
)

# --- FX.40 part 1 (owner decision Q22=A) — scheduler-data pseudonymisation ---
# The scheduler retrains on api_task_queue (+ task_resource_samples /
# prediction_logs joined through it). Rows older than the pseudonymise window
# get a keyed HMAC pseudonym of their tenant so the training corpus can be
# keyed by pseudonym instead of by tenant_id. The pseudonym KEY lives only in
# Vault (infrastructure/scheduler/pseudonym_key); with no key the sweep logs a
# WARNING and changes nothing (it never generates or prints a key).

register(
    "scheduler.pseudonymise_after_days",
    int,
    90,
    group="privacy",
    description="FX.40 part 1 (Q22=A) — age (days) past which the daily "
                "scheduler-pseudonymise sweep stamps api_task_queue rows with "
                "tenant_pseudonym = HMAC-SHA256(Vault key, tenant_id) truncated "
                "to 32 hex chars (prefixed 'p_'). Rows younger than this keep "
                "their real tenant_id. Default 90.",
)

register(
    "scheduler.aggregate_after_months",
    int,
    24,
    group="privacy",
    description="FX.40 part 2 (Q22=A) — age (months) past which scheduler "
                "rows roll into tenant-free aggregates: the daily aggregate "
                "sweep (services/scheduler_pseudonymise.run_aggregate_sweep, "
                "gated by scheduler.aggregate_job_enabled) folds api_task_queue "
                "rows older than this into scheduler_training_aggregates and "
                "soft-marks them aggregated_at (no deletes, tenant_id kept). "
                "Q22 retention: pseudonymised rows kept <=24 months, then "
                "rolled up. Default 24.",
)

register(
    "scheduler.pseudonymise_job_enabled",
    bool,
    True,
    group="privacy",
    description="FX.40 part 1 (Q22=A) — master switch for the daily "
                "scheduler-pseudonymise sweep (services/scheduler.py APScheduler "
                "job 'scheduler_pseudonymise'). False = no row is ever touched. "
                "Default True; the sweep still does nothing while the Vault "
                "pseudonym key is absent.",
)

register(
    "scheduler.aggregate_job_enabled",
    bool,
    True,
    group="privacy",
    description="FX.40 part 2 (Q22=A) — master switch for the daily "
                "scheduler aggregate roll-up sweep (services/scheduler.py "
                "APScheduler job 'scheduler_aggregate', services/"
                "scheduler_pseudonymise.run_aggregate_sweep). Folds "
                "api_task_queue rows older than scheduler.aggregate_after_months "
                "into scheduler_training_aggregates and soft-marks them "
                "aggregated_at. No rows are ever deleted and tenant_id is never "
                "touched. Default True.",
)

# --- FX.84 part 2a (owner decision Q67=A) — on-prem Backups settings ------

BACKUP_OFFBOX_TARGETS = ("none", "s3", "nfs", "disk", "ssh")


def _validate_backup_schedule_cron(value: str) -> None:
    # Shape check only (5 cron fields); the scheduler interprets it.
    if not isinstance(value, str) or len(value.split()) != 5:
        raise ValueError(
            "backup.schedule_cron must be 5 whitespace-separated cron "
            f"fields (min hour dom month dow), got {value!r}")


def _validate_backup_offbox_target(value: str) -> None:
    if value not in BACKUP_OFFBOX_TARGETS:
        raise ValueError(
            "backup.offbox_target must be one of "
            f"{BACKUP_OFFBOX_TARGETS}, got {value!r}")


def _validate_backup_retention_days(value: int) -> None:
    if not 1 <= value <= 3650:
        raise ValueError(
            f"backup.retention_days must be between 1 and 3650 days, "
            f"got {value!r}")


def _validate_backup_monthly_keep(value: int) -> None:
    if not 0 <= value <= 480:
        raise ValueError(
            f"backup.monthly_keep must be between 0 and 480 months, "
            f"got {value!r}")


register(
    "backup.schedule_enabled",
    bool,
    True,
    group="gsec",
    description="FX.84 part 2a (Q67=A) — the platform backup scheduler runs "
                "backup sets on backup.schedule_cron when true (default ON "
                "so a nightly set exists after install). Scheduler WIRING "
                "is Planned (part 2b); the operator cron in the runbook "
                "remains the current execution path.",
)
register(
    "backup.schedule_cron",
    str,
    "0 2 * * *",
    validator=_validate_backup_schedule_cron,
    group="gsec",
    description="FX.84 part 2a — cron expression (5 fields) for the "
                "platform backup schedule. Default nightly 02:00. Read by "
                "the Planned platform_backup_sweep scheduler task.",
)
register(
    "backup.retention_days",
    int,
    35,
    validator=_validate_backup_retention_days,
    group="gsec",
    description="FX.84 part 2a — local backup-set retention window in days. "
                "backup.sh reads it as RETENTION_DAYS env override "
                "(default 35); expiry stays "
                "directory-level pruning of dated sets, never hard delete "
                "of individual store objects.",
)
register(
    "backup.monthly_keep",
    int,
    12,
    description="FX.84 part 2a — how many month-end backup sets to keep "
                "beyond the daily retention window (GSEC R-148 tiered "
                "expiry). 0 = no monthly tier. Enforced by the Planned "
                "scheduler/expiry wiring; default 12.",
    group="gsec",
    validator=_validate_backup_monthly_keep,
)
register(
    "backup.offbox_target",
    str,
    "none",
    validator=_validate_backup_offbox_target,
    group="gsec",
    description="FX.84 part 2a — off-box destination for backup sets: "
                "none|s3|nfs|disk|ssh. Default 'none' (local sets only). "
                "Credentials for s3/ssh are NEVER stored here — they stay "
                "in the Vault (lib/backup-offbox.sh reads them at run "
                "time); this setting only names the target.",
)
register(
    "backup.offbox_path",
    str,
    "",
    group="gsec",
    description="FX.84 part 2a — target path/bucket/prefix for "
                "backup.offbox_target (e.g. bucket name or mount path). "
                "No credentials, no key material — transport secrets live "
                "in the Vault.",
)
register(
    "backup.restore_test_monthly",
    bool,
    True,
    group="gsec",
    description="FX.84 part 2a — when true, a restore test is expected at "
                "least once per month (the restore_test ledger lines back it "
                "up). Read by the Planned restore-test schedule wiring; "
                "backup.restore_test_max_age_days stays the fine-grained "
                "conformance target.",
)

# --- FX.96 part 3 (owner decision Q82=A) — DEBUG_MODE auto-off ------------

def _validate_debug_auto_off(value: int) -> None:
    if int(value) <= 0:
        raise ValueError(
            "debug.auto_off_minutes must be a positive number of minutes")


register(
    "debug.auto_off_minutes",
    int,
    60,
    validator=_validate_debug_auto_off,
    group="gsec",
    description="GSEC FX.96 (Q82=A) — how long DEBUG_MODE stays on before it "
                "switches itself off. services.debug_mode.is_debug_mode() "
                "compares the live `platform.debug_mode` row's `updated_at` "
                "against this many minutes: once the row is older, debug "
                "logging reads as off even though the row still says true, "
                "so an operator who leaves debug logging armed cannot leave "
                "it open indefinitely. Default 60. Must be > 0. A DB row is "
                "required for the timer (the DEBUG_MODE env fallback has no "
                "timestamp, so env-driven debug logging stays as it always "
                "was — see the audit doc).",
)

# ---------------------------------------------------------------------------
# FX.101 part 1 (owner decision Q87=A) — tenant IP allowlist.
# Per-tenant CIDR storage rides the tenant-scoped rows of this registry
# (scope='tenant'): auth.tenant_ip_allowlist is written per tenant via
# set_setting(..., tenant_id=...) from PUT /v1/tenant/security/ip-allowlist
# (routers/tenant/security.py) and read by services/tenant_ip_allowlist.py
# through get_setting(key, db, tenant_id) — tenant row > platform row >
# default (empty list = allow all, the default posture).
# ---------------------------------------------------------------------------

def _validate_fx101_ip_allowlist(value: Any) -> None:
    """Every entry must be a parseable IPv4/IPv6 CIDR (bare host
    addresses are accepted and read as /32 or /128). Raises ValueError
    so set_setting maps the write to an error the router turns into a
    400."""
    import ipaddress
    if value is None:
        return
    if isinstance(value, str):
        value = [value]
    for entry in value:
        entry = str(entry).strip()
        if not entry:
            raise ValueError("empty CIDR entry in auth.tenant_ip_allowlist")
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"invalid CIDR in auth.tenant_ip_allowlist: {entry!r}"
            ) from exc


register(
    "auth.ip_allowlist_enabled",
    bool,
    True,
    group="gsec",
    description="FX.101 part 1 (Q87=A) — global kill switch for the "
                "tenant IP allowlist. When false, tenant IP restrictions "
                "are not enforced for ANY tenant. Default true. A "
                "tenant-scoped row may override per tenant (resolution "
                "order: tenant row > platform row > default). Read on "
                "every authenticated request by "
                "services/tenant_ip_allowlist.check_ip_allowed.",
)
register(
    "auth.tenant_ip_allowlist",
    list,
    [],
    validator=_validate_fx101_ip_allowlist,
    group="gsec",
    description="FX.101 part 1 (Q87=A) — per-tenant IP allowlist: a list "
                "of IPv4/IPv6 CIDR strings (e.g. ['10.0.0.0/8', "
                "'2001:db8::/32']). Written PER TENANT (scope='tenant') "
                "by PUT /v1/tenant/security/ip-allowlist. EMPTY or "
                "absent = allow all (default, no restriction). When "
                "non-empty, API access from outside every listed CIDR "
                "is refused with 403 + a TENANT_IP_BLOCKED audit row. "
                "The real client IP is resolved with the single "
                "trusted-proxy-aware resolver (utils/client_ip.py); "
                "raw X-Forwarded-For is never trusted.",
)

# --- GSEC FX.153 part 2 (owner decision Q140=B) — staff register ---


def _validate_staff_training_interval_days(value: int) -> None:
    if not 1 <= value <= 3650:
        raise ValueError(
            f"staff.training_interval_days must be between 1 and 3650 "
            f"days, got {value!r}")


def _validate_staff_leaver_removal_hours(value: int) -> None:
    if not 1 <= value <= 720:
        raise ValueError(
            f"staff.leaver_access_removal_hours must be between 1 and 720 "
            f"hours, got {value!r}")


register(
    "staff.training_interval_days",
    int,
    365,
    validator=_validate_staff_training_interval_days,
    group="gsec",
    description="GSEC FX.153 part 2 (Q140=B) — how often a staff member's "
                "disciplinary training must be (re)completed. A staff "
                "record is training-due when training_completed_at is "
                "NULL or older than this many days (policy 21 "
                "personnel-security: training records). Read by "
                "services/staff_register.py GET /due and the daily "
                "training/leaver admin-email scheduler tick.",
)
register(
    "staff.leaver_access_removal_hours",
    int,
    24,
    validator=_validate_staff_leaver_removal_hours,
    group="gsec",
    description="GSEC FX.153 part 2 (Q140=B) — the leaver-checklist "
                "window: when a departure is known, ALL access (the 7 "
                "policy systems: keycloak, vault, harbor, git, tailnet, "
                "cloud_accounts, colo_access_list) must be removed within "
                "this many hours of leaver_at. A leaver whose checklist "
                "is still incomplete past this window is overdue — "
                "services/staff_register.py GET /due + the daily admin "
                "email. Default 24 (the policy's 'within 24 hours').",
)
register(
    "staff.due_email_enabled",
    bool,
    True,
    group="gsec",
    description="GSEC FX.153 part 2 (Q140=B) — master switch for the daily "
                "staff-register due email (services/scheduler.py "
                "staff_register_due_email_tick -> services/staff_register.py "
                "admin_email_tick): emails every ACTIVE platform admin the "
                "current GET /platform/staff-records/due list (training-due "
                "staff + leavers whose 7-system access-removal checklist is "
                "still incomplete past staff.leaver_access_removal_hours). "
                "Nothing is sent when nothing is due. Set False to freeze "
                "the email (the tick becomes a no-op).",
)

# --- FX.57 part 1 (owner decision Q40=A) — system log retention --------
# The platform's persisted system log store is `wizard_run_logs`
# (system/wizard run log lines — NOT customer content; every line is
# pre-redacted before it is written). The nightly job
# (services/system_log_archive.py, 03:55 UTC) exports rows older than
# logs.searchable_days as gzip JSONL + a SHA-256 manifest into the
# platform `system-logs` bucket, verifies the upload (size + hash via
# stat_object), and ONLY THEN stamps WizardRunLog.archived_at. Rows
# older than logs.archive_days are soft-marked too, never hard-deleted
# (ISO 27001). Gov/DoD posture: the cmmc / fedramp_high_il5 compliance
# profiles require logs.searchable_days >= 365 and logs.archive_days >=
# 548 (services/compliance_profiles.py PROFILE_REQUIRED_SETTINGS), so
# applying a gov profile writes 365/548 and the profile lock refuses
# any later weakening.
register(
    "logs.searchable_days",
    int,
    90,
    group="logging",
    description="FX.57 (Q40=A) — days a system log line stays in the "
                "online searchable window. The nightly system-log "
                "archive job exports rows older than this (gzip JSONL "
                "+ SHA-256 manifest to the system-logs bucket) and "
                "then soft-marks them archived. Rows are NEVER "
                "hard-deleted (ISO 27001). Default 90.",
)
register(
    "logs.archive_days",
    int,
    365,
    group="logging",
    description="FX.57 (Q40=A) — total online retention floor in "
                "days for system log rows past the searchable window. "
                "Rows older than this are soft-marked archived as well "
                "(mark only, never delete). Default 365; gov/DoD "
                "profiles (cmmc / fedramp_high_il5) require >= 548.",
)
register(
    "logs.archive_job_enabled",
    bool,
    True,
    group="logging",
    description="FX.57 (Q40=A) — enable the nightly system-log archive "
                "job (exports pre-searchable-window rows to the "
                "system-logs bucket + verifies + soft-marks). "
                "Default True: retention archiving is a compliance "
                "posture, not an opt-in; disabling it only stops the "
                "export (rows still age in place, never deleted).",
)

# ---------------------------------------------------------------------------


# --- FX.67 part 1 (owner decision Q50=A) — customer lockbox ------------

def _validate_lockbox_minutes(value: int, positive_min: int = 1) -> None:
    if int(value) < positive_min:
        raise ValueError(f"lockbox minutes must be >= {positive_min}")


register(
    "lockbox.enabled",
    bool,
    True,
    group="gsec",
    description="FX.67 (Q50=A) — customer lockbox: when True, platform "
                "staff may not read/write another tenant's CONTENT "
                "(workflows, files, outputs, run inputs) without an "
                "approved, unexpired, in-scope lockbox grant. Break-"
                "glass remains the sole bypass. Default True.",
)
register(
    "lockbox.default_minutes",
    int,
    240,
    validator=_validate_lockbox_minutes,
    group="gsec",
    description="FX.67 (Q50=A) — default grant duration in minutes when a "
                "staff member files a lockbox request without specifying "
                "one (240 = 4 h). Must be > 0 and <= lockbox.max_minutes.",
)
register(
    "lockbox.max_minutes",
    int,
    1440,
    validator=_validate_lockbox_minutes,
    group="gsec",
    description="FX.67 (Q50=A) — ceiling on a single lockbox grant "
                "duration in minutes (1440 = 24 h). Longer access needs "
                "a fresh request. Must be > 0.",
)

# --- FX.155 part 2a (owner decision Q142=A) — share-link horizon + master
# switch for EXTERNAL (unauthenticated) links.
# share.link_max_days is the clamp cog share links actually take:
# services/share_link_policy.get_policy resolves the effective expiry as
# min(share.max_expiry_days, share.link_max_days), so the legacy 30-day
# alias keeps working but can never outlive the 7-day default.
# share.external_links_enabled is the master switch on the outbound
# unauthenticated surface: when False, creating a cog share link is
# refused (403) and every existing link stops serving at visit/download
# time (403). Gov/DoD posture: the cmmc and fedramp_high_il5 compliance
# profiles (services/compliance_profiles.PROFILE_REQUIRED_SETTINGS)
# require it False — applying a profile writes it and check_profile_lock
# rejects any later write that re-enables it.
register(
    "share.link_max_days",
    int,
    7,
    validator=_validate_share_max_expiry_days,
    group="share",
    description="FX.155 (Q142=A) — maximum lifetime of any external "
                "share link, in days. Cog share links clamp their "
                "expiry to min(share.max_expiry_days, this), so the "
                "legacy share.max_expiry_days alias can never outlive "
                "this horizon. Default 7.",
)
register(
    "share.external_links_enabled",
    bool,
    True,
    group="share",
    description="FX.155 (Q142=A) — master switch for every "
                "unauthenticated outbound link (cog share links). "
                "When False, creating a share link is refused (403) "
                "and existing links stop serving (403 at visit/"
                "download). Required False by the cmmc and fedramp_"
                "high_il5 compliance profiles (gov/DoD posture). "
                "Default True.",
)

# ---------------------------------------------------------------------------
# GSEC FX.156 part 1 (owner decision Q143=B) — platform security
# alert-rules engine (services/security_alert_engine.py). One
# alert-rules engine over the platform's audit/log events: sign-in
# anomalies, admin + privileged actions, unusual downloads, cert/key
# expiry, backup failures, malware hits. Thresholds are DB settings,
# not env. The 5-minute scheduler sweep (services/scheduler.py job
# fx156_security_alert_rules) dedupes findings into security_alerts by
# (rule_key, tenant_id, subject). Email alerts + daily digest + ack
# endpoint arrive in PART 2; the remaining rules in PART 3 (each adds
# a registry entry + optional setting here). On-prem runs the SAME
# engine — the customer security contact is just another recipient.
# ---------------------------------------------------------------------------
register(
    "security_alerts.enabled",
    bool,
    True,
    group="security",
    description="GSEC FX.156 part 1 — master "
                "switch for the platform security alert-rules engine. "
                "When False, run_rules() returns immediately and no "
                "alerts are raised (the audit rows are ALWAYS written "
                "by their own producers — this only gates alerting). "
                "Default True.",
)
register(
    "security_alerts.signin_failure_threshold",
    int,
    10,
    group="security",
    description="GSEC FX.156 part 1 — failed sign-ins per user/IP "
                "required to raise the signin_failures alert, counted "
                "over the signin_failure_window_minutes sliding "
                "window. <= 0 disables the rule. Default 10.",
)
register(
    "security_alerts.signin_failure_window_minutes",
    int,
    15,
    group="security",
    description="GSEC FX.156 part 1 — sliding window (minutes) over "
                "which failed sign-ins are counted per user/IP for "
                "the signin_failures rule. Default 15.",
)

# ---------------------------------------------------------------------------
# GSEC FX.156 part 2 — security alert DELIVERY (services/
# security_alert_delivery.py). Immediate email for NEW alerts at/above
# security_alerts.email_min_severity (sent from the fx156 5-minute tick,
# after run_rules) + the daily 07:10 UTC digest of unacked alerts from
# the last 24h (scheduler job fx156_security_alert_digest).
# ---------------------------------------------------------------------------
register(
    "security_alerts.email_min_severity",
    str,
    "high",
    group="security",
    description="GSEC FX.156 part 2 — minimum severity for the IMMEDIATE "
                "security-alert email (low | medium | high | critical). "
                "NEW alerts at or above this rank are emailed at once: "
                "platform-level alerts go to every active platform admin, "
                "tenant alerts to that tenant's security recipients "
                "(stored contact -> secondary -> admin fallback, FX.89). "
                "Deduped repeats are NOT re-emailed (each alert emails "
                "once). Default \"high\".",
)
register(
    "security_alerts.digest_enabled",
    bool,
    True,
    group="security",
    description="GSEC FX.156 part 2 — master switch for the daily "
                "security-alerts digest email (07:10 UTC, scheduler job "
                "fx156_security_alert_digest). When True, one summary "
                "email per recipient group lists every UNACKED alert "
                "seen in the last 24h; when False the digest is skipped "
                "(immediate emails are unaffected). Default True.",
)


# ---------------------------------------------------------------------------
# GSEC FX.151 part 1b — SSRF-guarded HTTP client (utils/safe_http.py).
# The client refuses private / loopback / link-local (incl. the
# 169.254.169.254 cloud-metadata endpoint) / CGNAT 100.64/10 / multicast /
# reserved / unspecified / IPv6-ULA / IPv6-link-local targets by default.
# A host reached through this allowlist (exact hostname or CIDR) is the
# only way an outbound fetch may proceed to such a target, and every
# allowlisted fetch writes ONE audit row (SSRF_ALLOWLIST_FETCH) carrying
# the host only — never the full URL, which may carry tokens. Default
# [] = nothing allowlisted (fail closed).
# ---------------------------------------------------------------------------
def _validate_ssrf_allowlist(value: Any) -> None:
    """Reject a non-list, and any entry that is neither a hostname nor a
    well-formed CIDR/network."""
    import ipaddress as _ipaddress
    import re as _re

    if not isinstance(value, (list, tuple)):
        raise ValueError(
            "security.ssrf_allowlist must be a list of hostnames/CIDRs, "
            f"got {type(value).__name__}"
        )
    for entry in value:
        e = str(entry).strip().lower()
        if not e:
            continue
        if "/" in e:
            try:
                _ipaddress.ip_network(e, strict=False)
            except ValueError:
                raise ValueError(
                    f"security.ssrf_allowlist entry {entry!r} is not a "
                    "valid CIDR/network"
                ) from None
        else:
            # Either a plain hostname or a bare IP literal (no netmask).
            try:
                _ipaddress.ip_address(e)
                continue
            except ValueError:
                pass
            if not _re.match(
                r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$",
                e,
            ):
                raise ValueError(
                    f"security.ssrf_allowlist entry {entry!r} is neither a "
                    "hostname nor a valid CIDR/IP"
                )


register(
    "security.ssrf_allowlist",
    list,
    [],
    group="security",
    validator=_validate_ssrf_allowlist,
    description="GSEC FX.151 part 1b — hostnames/CIDRs that the SSRF "
                "guard (utils/safe_http.py) permits to be fetched even "
                "though they resolve to a private/loopback/link-local/"
                "CGNAT/reserved address. Every allowlisted fetch writes "
                "an SSRF_ALLOWLIST_FETCH audit row (host only, never the "
                "URL). Default [] (nothing allowlisted, fail closed).",
)

# GSEC FX.152 PART 1 (owner decision Q139=B) — API-key hygiene (group "keys").
#
# Part 1 is the scannable key format (services/api_key_format.py) plus the
# stale-key REPORT. Reporting only: nothing here disables a key. The owner
# decision is explicit that stale keys are reported to the tenant admin and
# NEVER auto-disabled — the FX.114 auto-disable job is a separate, already
# gated mechanism and is not what these keys drive.
#
# Part 2 (rotation endpoint + 24h grace, reminder emails, Dashboard button,
# stale-report email/scheduler) reads api_keys.rotation_reminder_days and
# api_keys.rotation_grace_hours registered here. No env vars: DB platform
# settings only.
# ---------------------------------------------------------------------------
def _validate_api_keys_stale_after_days(value: int) -> None:
    if not (1 <= int(value) <= 3650):
        raise ValueError(
            "api_keys.stale_after_days must be in 1..3650, got "
            f"{value!r}")


def _validate_api_keys_rotation_reminder_days(value: str) -> None:
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    if not parts:
        raise ValueError(
            "api_keys.rotation_reminder_days must be a comma-separated "
            f"list of positive integers, got {value!r}")
    for part in parts:
        try:
            days = int(part)
        except ValueError:
            raise ValueError(
                "api_keys.rotation_reminder_days entries must be positive "
                f"integers, got {part!r} in {value!r}") from None
        if days <= 0:
            raise ValueError(
                "api_keys.rotation_reminder_days entries must be positive "
                f"integers, got {part!r} in {value!r}")


def _validate_api_keys_rotation_grace_hours(value: int) -> None:
    if not (1 <= int(value) <= 168):
        raise ValueError(
            "api_keys.rotation_grace_hours must be in 1..168, got "
            f"{value!r}")


register(
    "api_keys.stale_after_days",
    int,
    90,
    validator=_validate_api_keys_stale_after_days,
    group="keys",
    description="FX.152 (Q139=B) — a tenant API key counts as stale when it "
                "has not been used for this many days (a key that has never "
                "been used is aged from its creation). The stale-key report "
                "(services/api_key_stale_report.py) lists those keys for the "
                "tenant admin. REPORTING ONLY — stale keys are never "
                "auto-disabled by this setting. Default 90.",
)
register(
    "api_keys.rotation_reminder_days",
    str,
    "14,3",
    validator=_validate_api_keys_rotation_reminder_days,
    group="keys",
    description="FX.152 (Q139=B) — comma-separated list of positive day "
                "offsets before a key's expiry at which the tenant admin is "
                "sent a rotation reminder (default '14,3' = 14 days and 3 "
                "days before expiry). One reminder per offset per key, ever. "
                "The send path is part 2; part 1 registers the tunable.",
)
register(
    "api_keys.rotation_grace_hours",
    int,
    24,
    validator=_validate_api_keys_rotation_grace_hours,
    group="keys",
    description="FX.152 (Q139=B) — how long the OLD key stays valid after a "
                "tenant-admin rotation (default 24, max 168). The rotate "
                "endpoint issues the new key, keeps the old one working for "
                "this window, then disables it; both keys write an audit "
                "trail row. The "
                "endpoint itself is part 2; part 1 registers the tunable.",
)

# ---------------------------------------------------------------------------
# GSEC FX.156 part 3 — platform security alert engine, remaining rules
# (services/security_alert_engine.py PART 3 block:
# ip_cross_account_failures / security_admin_actions /
# unusual_downloads / cert_expiry / backup_failure / malware_hits).
# Only the threshold-style tunables live here; the part-1 signin
# window/threshold and part-2 delivery settings are the blocks above.
# ---------------------------------------------------------------------------
register(
    "security_alerts.ip_cross_account_failure_threshold",
    int,
    5,
    group="security",
    description="GSEC FX.156 part 3 — distinct accounts a single source "
                "IP must hit with FAILED sign-ins (over the part-1 "
                "signin_failure_window_minutes window) to raise the "
                "ip_cross_account_failures alert (credential stuffing; "
                "the part-1 signin_failures rule groups per user/IP and "
                "does not catch one IP spanning many accounts). <= 0 "
                "disables the rule. Default 5.",
)
register(
    "security_alerts.download_count_threshold",
    int,
    50,
    group="security",
    description="GSEC FX.156 part 3 — per-user download/export audit "
                "count (content_download, content_download_presigned, "
                "trained_model_downloaded, admin_data_export) over a "
                "fixed trailing 1h window required to raise the "
                "unusual_downloads alert (medium). <= 0 disables the "
                "rule. Default 50.",
)
register(
    "security_alerts.cert_realert_hours",
    int,
    24,
    group="security",
    description="GSEC FX.156 part 3 — the cert_expiry rule re-alarms "
                "the same certificate at most once per this window "
                "(hours): while a live cert alert was seen inside the "
                "window, new CERT_EXPIRY_WARNING rows for that cert "
                "are skipped entirely. Default 24.",
)


# ---------------------------------------------------------------------------
# GSEC FX.57 part 3 — container log rotation
# ---------------------------------------------------------------------------
#
# The on-disk SIZE policy for stack/node container stdout logs. The worker's
# json-file driver has no size cap by default, so an unbounded stack can fill
# the worker disk; FX.57 part 3 stamps a bounded json-file logging block onto
# every stack service at deploy time (_compose_yaml_with_vars /
# _stamp_container_logging) using these two values.
#
# These are size caps ONLY. Time-based retention (searchable_days / archive_days)
# is the nightly DB system-log archive job's concern (FX.57 p1/p2); rotating
# container stdout never deletes an audit record (audit lives in Postgres /
# Vault, not container stdout). See docs/runbooks/container-log-rotation.md.
register(
    "logging.container_max_size",
    str,
    "50m",
    group="logging",
    description="GSEC FX.57 part 3 — max-size for the json-file logging driver "
                "stamped onto every stack service at deploy. Size cap only "
                "(e.g. '50m', '100m'); retention is the nightly system-log "
                "archive job, not this knob.",
)
register(
    "logging.container_max_files",
    int,
    5,
    group="logging",
    description="GSEC FX.57 part 3 — max-file (how many rotated log files to "
                "keep) for the json-file logging driver stamped onto every "
                "stack service at deploy. Pairs with logging.container_max_size.",
)


# ---------------------------------------------------------------------------
# GSEC FX.59 part 2 — contract-end tenant lifecycle (owner decision Q42=A)
# ---------------------------------------------------------------------------
#
# Timings for the hosted-cloud contract-end state machine
# (utils/tenant_lifecycle.py): days 0-30 after contract end the tenant is
# 'read_only' (writes blocked, reads + self-serve export open); at the end
# of the window it becomes 'suspended' (reactivation inside the grace
# period restores everything); the day-60 crypto-shred timing is part 5's
# concern and will register its own key when it ships. On-prem tenants run
# the same state machine with customer-chosen timings — these settings are
# the defaults that seed a new on-prem deployment.
register(
    "tenant_lifecycle.read_only_days",
    int,
    30,
    group="tenant_lifecycle",
    description="GSEC FX.59 part 2 (Q42=A) — days after contract end during "
                "which the tenant stays 'read_only' (writes blocked, reads "
                "and self-serve export open) before it is suspended. "
                "Default 30.",
)
register(
    "tenant_lifecycle.export_window_days",
    int,
    30,
    group="tenant_lifecycle",
    description="GSEC FX.59 part 2 (Q42=A) — days after contract end during "
                "which the self-serve full export (workflows, files, "
                "outputs, audit log) stays available. Runs alongside the "
                "read_only window; default 30.",
)


# ---------------------------------------------------------------------------
# GSEC FX.49 part 1 (owner decision Q32=A) — break-glass custody
# ---------------------------------------------------------------------------
#
# The break-glass credential is SPLIT (one half sealed in Vault behind
# two-person approval, the other half printed in a sealed envelope in the
# owner's physical safe) and a use request needs a SECOND platform admin's
# approval that lapses after this window (services/break_glass_custody.py).
# Part 2 builds on the same block: alert delivery + forced rotation.

def _validate_break_glass_minutes(value: int, positive_min: int = 5) -> None:
    if int(value) < positive_min:
        raise ValueError(f"break-glass minutes must be >= {positive_min}")


register(
    "break_glass.enabled",
    bool,
    True,
    group="gsec",
    description="FX.49 (Q32=A) — break-glass custody master switch. When "
                "True, a break-glass credential use requires an approved, "
                "unexpired, single-use use-request (two-person approval). "
                "Default True.",
)
register(
    "break_glass.approval_window_minutes",
    int,
    60,
    validator=_validate_break_glass_minutes,
    group="gsec",
    description="FX.49 (Q32=A) — how long an approved break-glass use-"
                "request stays usable before it is swept to 'expired' "
                "(minutes from approval). Must be >= 5.",
)

# ---------------------------------------------------------------------------
# GSEC FX.50 part 2 — breach-notification incident tracker deadlines
# (services/security_incidents.py)
# ---------------------------------------------------------------------------
#
# Q33=A: affected customers are told within 72 hours of CONFIRMING a
# breach; tenants on a gov/DoD compliance profile (cmmc /
# fedramp_high_il5) get an initial heads-up within 24 hours. Both
# clocks are stamped on the incident row at confirm time from these
# two knobs (services/security_incidents.confirm_incident); the
# overdue report selects rows whose clock has passed with notified_at
# still NULL. Email delivery + the in-app banner are a later part
# (FX.50 p3).

def _validate_incident_deadline_hours(value: int) -> None:
    if not (1 <= int(value) <= 240):
        raise ValueError(
            "incident deadline hours must be in 1..240, got "
            f"{value!r}")


register(
    "incident.breach_notify_hours",
    int,
    72,
    validator=_validate_incident_deadline_hours,
    group="security",
    description="GSEC FX.50 part 2 (Q33=A) — hours after an incident "
                "is CONFIRMED by which the affected tenant must be "
                "notified (notify_due_at = confirmed_at + this). "
                "Written into the DPA/ToS as 'within 72 hours'. "
                "Default 72.",
)
register(
    "incident.government_headsup_hours",
    int,
    24,
    validator=_validate_incident_deadline_hours,
    group="security",
    description="GSEC FX.50 part 2 (Q33=A) — hours after an incident "
                "is confirmed by which tenants on a gov/DoD "
                "compliance profile (cmmc / fedramp_high_il5) get the "
                "initial heads-up (gov_headsup_due_at = confirmed_at + "
                "this). Only stamped on installs with an active gov "
                "profile; ignored otherwise. Default 24.",
)
# GSEC FX.72 part 2 (owner decision Q55=C) — no AI training without opt-in.
# Customer content (files, prompts, workflow data, outputs) is never used to
# train or fine-tune any model unless the TENANT explicitly opts in. Default
# False = opted OUT: any customer-training node (routers/worker_tasks.
# _TRAINING_NODE_KINDS — the SDXL LoRA trainers) is refused at run start AND
# at the enqueue choke point (services/ai_training_consent.
# check_run_training_opt_in / check_task_training_opt_in, 403 + audit).
# Platform scheduler self-tuning (services/ml_retrain, task_type='system')
# and scheduler timing telemetry (routers/training, routers/calibration) are
# NOT customer content and are deliberately not gated here (Q22 / FX.40).
register(
    "ai.training_opt_in",
    bool,
    False,
    group="ai",
    description="GSEC FX.72 p2 (Q55=C): when True AT TENANT SCOPE, that "
                "tenant allows model-training nodes on its own content. "
                "Default False (opted out) — training dispatch is refused "
                "403 + audit unless the tenant opts in. Read by "
                "services/ai_training_consent.py.",
)


# ---------------------------------------------------------------------------
# GSEC FX.70 part 2 (owner decision Q53=A) — support response targets.
# The SINGLE (plan_tier -> severity -> hours) table for the in-app
# support-ticket SLA (services/support_tickets.py). plan_tier keys are
# Plan.tier values (the tier names utils.entitlements defines;
# NULL = community). Hours until FIRST response is due: the owner on-call tier sev1
# 1h (24/7, owner on-call), sev2 4 business hours, sev3 1 business day,
# sev4 2 business days; other staffed tiers sev1 4 business hours, sev2-4 1-2
# business days; community best effort (null = no SLA due time,
# forum/GitHub). Business hours = Mon-Fri 9-17 UTC (no holidays).
# ---------------------------------------------------------------------------

def _validate_support_response_targets(value: dict) -> None:
    if not isinstance(value, dict) or not value:
        raise ValueError("support response targets must be a non-empty "
                         "plan_tier -> severity -> hours dict")
    for tier, row in value.items():
        if not isinstance(row, dict):
            raise ValueError(f"plan_tier {tier!r} must map to a dict of "
                             "severity -> hours")
        for sev, hours in row.items():
            if hours is not None and (
                    not isinstance(hours, (int, float)) or hours <= 0):
                raise ValueError(
                    f"support response target for {tier!r}/{sev!r} must be "
                    "a positive number of hours or null (best effort)")


register(
    "support_tickets.response_targets",
    dict,
    {
        "team":       {"sev1": 4.0, "sev2": 8.0, "sev3": 8.0, "sev4": 16.0},
        "business":   {"sev1": 4.0, "sev2": 8.0, "sev3": 8.0, "sev4": 16.0},
        "community":  {"sev1": None, "sev2": None, "sev3": None, "sev4": None},
    },
    validator=_validate_support_response_targets,
    group="gsec",
    description="GSEC FX.70 p2 (Q53=A): plan_tier -> severity -> hours "
                "until a support ticket's first response is due. "
                "Owner-on-call tier sev1 1h 24/7; staffed tiers sev1 4 business hours, "
                "sev2-4 1-2 business days; community best effort (null). "
                "Business hours Mon-Fri 9-17 UTC. Read by "
                "services/support_tickets.py.",
)

# GSEC FX.69 part 2 (owner decision Q52=A) — hosted-uptime SLA sampling.
# The scheduler job (services/scheduler.py, id "fx69_uptime_sampling")
# records one probe sample per component per minute from the /health
# probe fan-out via services/uptime.run_sampling_tick. Default True =
# on.
register(
    "uptime.sampling_enabled",
    bool,
    True,
    group="gsec",
    description="GSEC FX.69 p2 (Q52=A): master switch for the uptime "
                "sampling job (one /health probe sample per component "
                "per minute). Feeds the monthly uptime SLA calculation "
                "(services/uptime.py). Read by services/scheduler.py.",
)

# ---------------------------------------------------------------------------
# GSEC FX.63 p2 (owner Q46=A + amendment) — outbound-data kill switches.
# Registry default = the PAID on-prem answer (Q46=A: a paid on-prem install
# sends NOTHING by default). MOD CE flips telemetry.usage_stats ON at first
# run (the amendment: anonymous usage stats are ON by default, opt-out); the
# CE default is derived in services/telemetry_payload.py (the registry
# cannot hold an edition-dependent constant), and the FedRAMP high-IL5
# profile hard-blocks both regardless of what any admin sets.
# ---------------------------------------------------------------------------
register(
    "telemetry.update_check",
    bool,
    False,
    group="telemetry",
    description="GSEC FX.63 p2 (Q46=A) — allow version/update-check egress "
                "from this install. CE default OFF (no sender ships in this edition; see services/telemetry_"
                "payload.py). When False, no update-check request is ever "
                "built or sent. GET /v1/admin/telemetry/preview shows the "
                "exact payload either way. Hard-blocked by the fedramp_"
                "high_il5 compliance profile.",
)
register(
    "telemetry.usage_stats",
    bool,
    False,
    group="telemetry",
    description="GSEC FX.63 p2 (Q46=A + amendment) — allow anonymous usage-"
                "stats egress. MOD CE default OFF (no sender ships in this edition) "
                "Payload (paid editions) is non-content "
                "only: version, edition, counts — never tenant names, file "
                "names, prompts, emails or IPs (see services/"
                "telemetry_payload.py build_payload). Hard-blocked by the "
                "fedramp_high_il5 compliance profile.",
)

# ---------------------------------------------------------------------------
# GSEC FX.152 part 3 (owner decision Q139=B) — API-key reminder emails.
# Two gates read by services/api_key_reminders.py on every scheduler tick
# (turning either off needs no restart). The day horizons themselves are the
# existing api_keys.rotation_reminder_days setting, and the stale window is
# api_keys.stale_after_days — no new tunables here, only the on/off gates.
# Neither gate can disable a key: the owner decision is explicit that stale
# keys are REPORTED to tenant admins and never auto-disabled.
# ---------------------------------------------------------------------------
register(
    "api_keys.expiry_reminders_enabled",
    bool,
    True,
    group="keys",
    description="GSEC FX.152 p3 (Q139=B) — send the API-key expiry reminder "
                "emails at the api_keys.rotation_reminder_days horizons "
                "(default 14 and 3 days before expiry), one per key per "
                "horizon, ever. Sent to the key owner and the tenant admins "
                "through the existing email service. Default ON.",
)
register(
    "api_keys.stale_report_enabled",
    bool,
    True,
    group="keys",
    description="GSEC FX.152 p3 (Q139=B) — send the weekly stale-key digest "
                "email to each tenant's admins (keys unused for "
                "api_keys.stale_after_days, default 90). One digest per "
                "tenant per week, deduped through lifecycle_email_log. "
                "REPORTING ONLY — stale keys are never auto-disabled by "
                "this setting. Default ON.",
)


# ---------------------------------------------------------------------------
# GSEC FX.74 part 2 (owner decisions Q57=A / Q119=A) — export control:
# sanctioned-country block at hosted signup and paid checkout.
#
# The list of blocked countries is a PLATFORM SETTING (no env var, no new
# table column) so counsel/admin can retune it without a code change.
# Codes are ISO 3166-1 alpha-2, uppercase. Default = the US-government
# "sanctioned countries" list BIS/NSA CE export control keys off (Q57=A
# "hosted-cloud signup and paid checkout block sanctioned countries (IP +
# billing country)"). Enforcement lives in services/sanctions_gate.py.
# ---------------------------------------------------------------------------

_SANCTIONED_DEFAULT = [
    "AF", "MM", "IR", "IQ", "IL", "JO", "LB", "LY", "NG", "KP",
    "PK", "SY", "TR", "VE", "YE", "ZW", "CU", "EG", "IN", "SD",
    "TM", "EH",
]


def _validate_sanctioned_countries(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("sanctions.blocked_countries must be a list of "
                         "ISO 3166-1 alpha-2 country codes")
    for code in value:
        if not isinstance(code, str) or len(code) != 2 or not code.isalpha():
            raise ValueError(
                f"sanctions.blocked_countries entry {code!r} is not a "
                "2-letter ISO 3166-1 alpha-2 code")


register(
    "sanctions.blocked_countries",
    list,
    _SANCTIONED_DEFAULT,
    group="gsec",
    validator=_validate_sanctioned_countries,
    description="GSEC FX.74 p2 (Q57=A): ISO 3166-1 alpha-2 country codes "
                "blocked at hosted signup and paid checkout (IP country + "
                "billing country). Default = the US-government sanctioned-"
                "countries list BIS/NSA CE export control keys off. Read "
                "by services/sanctions_gate.py.",
)
register(
    "sanctions.enabled",
    bool,
    True,
    group="gsec",
    description="GSEC FX.74 p2 (Q57=A): kill switch for the sanctioned-"
                "country gate. Default True on SaaS (hosted signup + paid "
                "checkout refuse blocked countries with 451 + audit). "
                "Always inert on-prem (self-hosted = no hosted signup/"
                "checkout). Read by services/sanctions_gate.py.",
)


# ---------------------------------------------------------------------------
# GSEC FX.63 part 1 (owner decisions Q46=A + amendment, Q23=A) — signed
# offline licence file: an EXPIRED (past grace) self-host licence makes the
# whole platform read-only rather than shutting anything down. Kill switch
# only — the licence file path / grace-window length are the pre-existing
# license.file_path / license.grace_days settings (utils/license_file.py);
# this module does not duplicate those.
# ---------------------------------------------------------------------------
register(
    "licence.readonly_on_expiry",
    bool,
    True,
    group="license",
    description="GSEC FX.63 p1 (Q23=A): when True (default), a self-host "
                "signed offline licence that has expired past its grace "
                "window (utils.license_file state 'expired') makes the "
                "platform read-only (423 on write routes via "
                "utils.tenant_lifecycle.require_tenant_writable) until a "
                "renewed licence is uploaded at POST /v1/admin/licence. "
                "Set False to disable the read-only consequence while "
                "still falling back to the baseline entitlements "
                "(utils.entitlements, unaffected by this switch). Always "
                "inert on SaaS (services/offline_licence.is_read_only).",
)


# ---------------------------------------------------------------------------
# GSEC FX.31 p2a — headscale preauth keys must be single-use with a ~1h TTL
# at every mint site. ONE DB-backed TTL knob instead of hardcoded ttl_hours
# literals scattered across utils/headscale_client.py mint call sites; read
# by utils.headscale_client.default_mint_kwargs()/preauth_key_ttl_minutes().
# ---------------------------------------------------------------------------

def _validate_preauth_key_ttl_minutes(value: int) -> None:
    if not 5 <= value <= 1440:
        raise ValueError(
            f"mesh.preauth_key_ttl_minutes must be 5-1440 minutes, got {value!r}"
        )


register(
    "mesh.preauth_key_ttl_minutes",
    int,
    60,
    group="gsec",
    validator=_validate_preauth_key_ttl_minutes,
    description="GSEC FX.31 p2a: TTL (minutes, 5-1440, default 60) for "
                "single-use (reusable=false) Headscale preauth keys minted "
                "at worker enrollment / stack mesh join / tailnet egress "
                "sidecar sites. Read by "
                "utils.headscale_client.preauth_key_ttl_minutes()/"
                "default_mint_kwargs(); every Python mint site should use "
                "that helper unless it intentionally mints a reusable key "
                "(documented inline where that happens).",
)


# ---------------------------------------------------------------------------
# GSEC FX.56 p1 (owner decision Q39=A) — credential lifetime defaults.
#
# Every new credential (user API keys, service-account keys, app keys)
# carries a FINITE expires_at: when the caller omits one, the default
# lifetime applies; a caller-supplied lifetime beyond the max is refused
# (422 CREDENTIAL_LIFETIME_TOO_LONG). Under an active gov/DoD compliance
# profile (cmmc / fedramp_high_il5) the max is capped at the gov value.
# Enforcement + the backfill of never-expiring live credentials live in
# services/credential_lifetime.py.
# ---------------------------------------------------------------------------
def _validate_credentials_lifetime(value: int) -> None:
    if value < 1:
        raise ValueError(
            "credentials.* lifetime settings must be >= 1 day, "
            f"got {value!r}")


register(
    "credentials.default_lifetime_days",
    int,
    90,
    group="credentials",
    validator=_validate_credentials_lifetime,
    description="GSEC FX.56 p1 (Q39=A) — default lifetime (days) applied to "
                "a newly minted credential when the caller omits an expiry "
                "(API keys, service-account keys, app keys). Default 90.",
)
register(
    "credentials.max_lifetime_days",
    int,
    365,
    group="credentials",
    validator=_validate_credentials_lifetime,
    description="GSEC FX.56 p1 (Q39=A) — maximum credential lifetime (days) "
                "the user may pick; longer requests are refused with 422 "
                "CREDENTIAL_LIFETIME_TOO_LONG. Default 365 (1 year).",
)
register(
    "credentials.gov_max_lifetime_days",
    int,
    90,
    group="credentials",
    validator=_validate_credentials_lifetime,
    description="GSEC FX.56 p1 (Q39=A) — credential lifetime ceiling (days) "
                "in force while a gov/DoD compliance profile (cmmc / "
                "fedramp_high_il5) is active: the effective max is the "
                "MINIMUM of credentials.max_lifetime_days and this value. "
                "Default 90.",
)


# ---------------------------------------------------------------------------
# GSEC FX.84 part 3a — off-box backup TRANSPORTS (s3 / nfs / disk), chosen
# at API-side copy time (services/backup_transports.py). The pre-existing
# backup.offbox_target (none|s3|nfs|disk|ssh) names the host-side
# backup.sh destination (lib/backup-offbox.sh); this group selects the
# transport the API's run_offbox_backup() hook actually uses to ship one
# finished local backup artifact, and where. Default "none" = no off-box
# copy from the API (host-side shipping via backup.sh is unaffected).
# Credentials are NEVER stored here: S3 creds stay in the Vault (part 3b
# provisions the write-only path the S3Transport client is built from);
# nfs/disk need no secrets (the mount/permissions ARE the boundary).
# ---------------------------------------------------------------------------

def _validate_backup_offbox_transport(value: str) -> None:
    if value not in ("none", "s3", "nfs", "disk"):
        raise ValueError(
            "backup.offbox.transport must be one of "
            f"('none', 's3', 'nfs', 'disk'), got {value!r}")


register(
    "backup.offbox.transport",
    str,
    "none",
    validator=_validate_backup_offbox_transport,
    group="gsec",
    description="GSEC FX.84 part 3a — which transport the API-side off-box "
                "backup copy (services/backup_transports.run_offbox_backup) "
                "uses after a successful local backup run: none (default, "
                "no off-box copy) | s3 | nfs | disk. Credentials are never "
                "stored here — S3 creds stay in the Vault (part 3b), nfs/"
                "disk need none (mount/permissions are the boundary).",
)
register(
    "backup.offbox.s3_bucket",
    str,
    "",
    group="gsec",
    description="GSEC FX.84 part 3a — bucket for backup.offbox.transport="
                "'s3' copies. No credentials, no key material; the "
                "write-only S3 creds live in the Vault (part 3b).",
)
register(
    "backup.offbox.s3_prefix",
    str,
    "",
    group="gsec",
    description="GSEC FX.84 part 3a — object prefix under "
                "backup.offbox.s3_bucket for off-box backup copies "
                "(e.g. 'platform-backups').",
)
register(
    "backup.offbox.path",
    str,
    "",
    group="gsec",
    description="GSEC FX.84 part 3a — root path for "
                "backup.offbox.transport 'nfs' / 'disk' copies: the NFS "
                "mount point or local disk root the transport writes into "
                "atomic tmp+rename copies. Relative names resolving "
                "outside this root are refused.",
)

# ---------------------------------------------------------------------------
# GSEC FX.58 s5 — encrypted off-site SECOND-region backup copies (storage
# only): an OPTIONAL second off-box transport target that reuses the SAME
# transport code path as the primary (services/backup_transports). Unset
# (transport "none") = behaviour byte-identical to FX.84. Credentials are
# NEVER stored here: an s3 secondary reads its write-only creds from the
# Vault path named by backup.offbox_secondary.s3_credentials_vault_path
# (the primary's live in infrastructure/backup-offbox); nfs/disk need no
# secrets (the mount/permissions are the boundary). backup.sh reads the
# transport/bucket/prefix/path keys through the API container (psr_get —
# the platform registry is the single source of truth, no per-box env for
# behaviour); the Vault path is a name, not a secret.
# ---------------------------------------------------------------------------

def _validate_backup_offbox_secondary_transport(value: str) -> None:
    if value not in ("none", "s3", "nfs", "disk"):
        raise ValueError(
            "backup.offbox_secondary.transport must be one of "
            f"('none', 's3', 'nfs', 'disk'), got {value!r}")


register(
    "backup.offbox_secondary.transport",
    str,
    "none",
    validator=_validate_backup_offbox_secondary_transport,
    group="gsec",
    description="GSEC FX.58 s5 — second off-box backup target (off-site "
                "second-region copies, storage only): none (default — no "
                "second target, behaviour identical to FX.84) | s3 | nfs | "
                "disk. Same transport code path as the primary "
                "(services/backup_transports.get_transport); backup.sh "
                "ships the SAME already-age-encrypted set to it, and the "
                "off-box probe + backup alerts report each target "
                "separately. Credentials are never stored here.",
)
register(
    "backup.offbox_secondary.s3_endpoint",
    str,
    "",
    group="gsec",
    description="GSEC FX.58 s5 — S3-compatible endpoint URL for "
                "backup.offbox_secondary.transport='s3' copies (the second "
                "region's host — public material, never a credential).",
)
register(
    "backup.offbox_secondary.s3_bucket",
    str,
    "",
    group="gsec",
    description="GSEC FX.58 s5 — bucket for "
                "backup.offbox_secondary.transport='s3' copies (a different "
                "region/endpoint than the primary's bucket). No "
                "credentials, no key material; the write-only creds live in "
                "the Vault at backup.offbox_secondary.s3_credentials_vault_"
                "path.",
)
register(
    "backup.offbox_secondary.s3_prefix",
    str,
    "",
    group="gsec",
    description="GSEC FX.58 s5 — object prefix under "
                "backup.offbox_secondary.s3_bucket for second-target backup "
                "copies (e.g. 'mod-backups-region2').",
)
register(
    "backup.offbox_secondary.path",
    str,
    "",
    group="gsec",
    description="GSEC FX.58 s5 — root path for "
                "backup.offbox_secondary.transport 'nfs' / 'disk' copies: "
                "the second region's NFS mount point or local disk root. "
                "Relative names resolving outside this root are refused.",
)
register(
    "backup.offbox_secondary.s3_credentials_vault_path",
    str,
    "infrastructure/backup-offbox-secondary",
    group="gsec",
    description="GSEC FX.58 s5 — Vault kv2 path (NOT a secret — a name) "
                "holding the SECOND target's write-only S3 credentials "
                "(endpoint, access_key, secret_key, region, secure), read "
                "exactly like the primary's infrastructure/backup-offbox. "
                "Ignored for nfs/disk. No credential value ever lives in a "
                "platform setting.",
)

# GSEC FX.59 p5a (day-60 tenant-key crypto shred) settings live in ONE
# block at the END of this file — see tenant.deletion_shred_after_days /
# tenant.crypto_shred_enabled registered there.

# GSEC FX.99 p2 — tenant-scoped commercial-safe-only run gate. This
# registry default (False) is NOT the effective default — resolution is
# dynamic: services/commercial_safe.py::commercial_safe_only_for() reads
# an explicit tenant/platform row via resolve_source() when one exists,
# else computes ON for hosted (SaaS) tenants on a subscription plan
# and OFF for CE / free / on-prem. The registry default here only backs
# the type coercion / admin-settings-page row when no plan-derived
# default applies (e.g. a direct get_setting() call with no tenant_id).
register(
    "models.commercial_safe_only",
    bool,
    False,
    group="gsec",
    description="GSEC FX.99 p2 — when on, a workflow run is refused "
                "(422) if its graph includes a node whose detected "
                "licence classifies as non_commercial. Default "
                "resolution (see services/commercial_safe.py) is ON "
                "for hosted subscription plans, OFF for CE / "
                "free / on-prem, unless a tenant admin set this "
                "setting explicitly.",
)

# GSEC FX.73 p3 — tenant-scoped acceptance of the non-commercial-licence
# terms. Tenant-scoped only (no platform-wide/plan-derived default): a
# paid/hosted tenant with models.commercial_safe_only ON hides
# non_commercial-licensed nodes from the palette and marketplace listing
# until a tenant admin accepts via services/commercial_safe.py::
# accept_noncommercial_terms (POST /v1/tenant/licence-terms/noncommercial/
# accept) — see services/commercial_safe.py::hide_noncommercial_for.
register(
    "models.noncommercial_terms_accepted",
    bool,
    False,
    group="gsec",
    description="GSEC FX.73 p3 — set True once a tenant admin accepts "
                "the non-commercial-licence terms for this tenant. "
                "While False on a tenant with models.commercial_safe_only "
                "ON, non_commercial-licensed nodes are hidden from the "
                "node palette and the marketplace listing (unknown "
                "licence stays visible). CE / on-prem / free tenants "
                "are unaffected.",
)


# --- GSEC FX.58 p1b (Q41=A, RPO target 1 hour) — hourly DB snapshots --------
# Retention window (in hours) for the lightweight hourly snapshots that
# `CORE/startup/backup.sh --snapshot` writes under <backup root>/
# snapshots/<UTC timestamp>/. Read by backup/snapshot-prune.sh the same
# way backup.sh reads backup.retention_days (docker exec against this
# registry); prune is confined to snapshots/. Same group as the other
# backup.* settings.
def _validate_backup_snapshot_retention_hours(value: int) -> None:
    if not 1 <= value <= 720:
        raise ValueError(
            f"backup.snapshot_retention_hours must be between 1 and 720 "
            f"hours, got {value!r}")


register(
    "backup.snapshot_retention_hours",
    int,
    48,
    validator=_validate_backup_snapshot_retention_hours,
    group="gsec",
    description="GSEC FX.58 p1b (Q41=A) — how many hours of hourly DB "
                "snapshots to keep (CORE/startup/backup.sh --snapshot + "
                "backup/snapshot-prune.sh; newest-N pruning, confined to "
                "the snapshots/ dir). Default 48.",
)


# ---------------------------------------------------------------------------
# GSEC FX.74 p3a — offline OFAC SDN NAME screening at hosted signup
# (matches are HELD for review, never blocked outright). Enforcement lives
# in services/sanctions_screening.py.
# ---------------------------------------------------------------------------

def _validate_sdn_path(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("sanctions.sdn_path must be a non-empty path "
                         "string (the OFAC SDN CSV)")


def _validate_screening_enabled(value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError("sanctions.screening_enabled must be a bool")


def _validate_match_threshold(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("sanctions.match_threshold must be a number")
    if not 0.0 < float(value) <= 1.0:
        raise ValueError("sanctions.match_threshold must be in (0, 1]")


register(
    "sanctions.sdn_path",
    str,
    "/app/CORE/Metadata/sanctions/sdn.csv",
    group="gsec",
    validator=_validate_sdn_path,
    description="GSEC FX.74 p3a: path to the OFAC SDN CSV used for offline "
                "name screening at hosted signup (an optional alt.csv with "
                "the same ent_num is read from the same directory). Read "
                "by services/sanctions_screening.py.",
)
register(
    "sanctions.screening_enabled",
    bool,
    False,
    group="gsec",
    validator=_validate_screening_enabled,
    description="GSEC FX.74 p3a: kill switch for offline OFAC SDN name "
                "screening at hosted signup. Default False (opt-in until "
                "the list + counsel review are in place). A match HELDS "
                "the account for review (audit row + user status PENDING); "
                "it is never blocked outright. Read by "
                "services/sanctions_screening.py.",
)
register(
    "sanctions.match_threshold",
    float,
    0.92,
    group="gsec",
    validator=_validate_match_threshold,
    description="GSEC FX.74 p3a: minimum difflib.SequenceMatcher ratio "
                "(0,1] over normalized names for an SDN screening match. "
                "Default 0.92. Read by services/sanctions_screening.py.",
)


# --- GSEC FX.65 p3a — BYOK revoke -> tenant "dark" detector ----------------
# A tenant's customer-managed KMS key (services/kms_providers.py /
# kms_aws.py / kms_azure.py). kms.byok_provider/kms.byok_key_ref are
# tenant-scoped and ADMIN-SET ONLY (set once when a tenant turns on BYOK);
# kms.revoked_dark is tenant-scoped but written ONLY by
# services/kms_tenant_state.py (record_unwrap/clear_dark) — the tenant
# settings-write routers must not expose it, same as any other
# system-managed tenant row. kms.health_interval_minutes is platform-only
# (services/scheduler.py's periodic health-probe cadence).
register(
    "kms.byok_provider",
    str,
    "",
    group="crypto",
    description="GSEC FX.65 p3a — name of the BYOK KMS provider this "
                "tenant uses (services.kms_providers registry, e.g. "
                "'aws'/'azure'), or '' for MOD-managed keys (Vault "
                "transit / local master key). Tenant-scoped, admin-set.",
)

register(
    "kms.byok_key_ref",
    str,
    "",
    group="crypto",
    description="GSEC FX.65 p3a — the tenant's key reference (ARN / "
                "Key Vault key id) within its kms.byok_provider. "
                "Tenant-scoped, admin-set.",
)

register(
    "kms.revoked_dark",
    bool,
    False,
    group="crypto",
    description="GSEC FX.65 p3a — True when this tenant's BYOK key has "
                "been refused by its KMS (revoked/deleted/denied) or "
                "failed a health probe. Written ONLY by "
                "services/kms_tenant_state.py (record_unwrap on a "
                "refusal; clear_dark to resolve) — read-only to tenants, "
                "never a direct settings-write target. While True, "
                "services.kms_tenant_state.require_not_dark refuses "
                "that tenant's requests with 423.",
)


def _validate_kms_health_interval_minutes(value: int) -> None:
    if not 1 <= value <= 60:
        raise ValueError(
            f"kms.health_interval_minutes must be between 1 and 60 "
            f"minutes, got {value!r}")


register(
    "kms.health_interval_minutes",
    int,
    2,
    validator=_validate_kms_health_interval_minutes,
    group="crypto",
    description="GSEC FX.65 p3a — cadence (minutes) for the scheduled "
                "BYOK key health probe (services.kms_tenant_state."
                "check_all, registered in services/scheduler.py). "
                "Platform-wide only. Default 2.",
)


# --- GSEC FX.64 p3a [B] — weekly curated base-image rebuild cadence --------
# Opt-in weekly cadence on top of the nightly dispatcher in
# utils/base_image_rebuild.py: services/base_image_weekly.run_weekly_rebuild
# is gated by security.base_rebuild_weekly_enabled and fires on the
# configured weekday/hour (UTC) via services/scheduler.py.
def _validate_base_rebuild_weekday(value: int) -> None:
    if not 0 <= value <= 6:
        raise ValueError(
            f"security.base_rebuild_weekday must be between 0 and 6, got {value!r}")


def _validate_base_rebuild_hour(value: int) -> None:
    if not 0 <= value <= 23:
        raise ValueError(
            f"security.base_rebuild_hour_utc must be between 0 and 23, got {value!r}")


register(
    "security.base_rebuild_weekly_enabled",
    bool,
    False,
    group="security",
    description="GSEC FX.64 p3a — enable the weekly curated base-image "
                "rebuild cadence (services/base_image_weekly.run_weekly_rebuild "
                "on top of the nightly utils/base_image_rebuild dispatcher). "
                "Off by default.",
)

register(
    "security.base_rebuild_weekday",
    int,
    6,
    validator=_validate_base_rebuild_weekday,
    group="security",
    description="GSEC FX.64 p3a — day of week (0=Mon..6=Sun) the weekly "
                "base-image rebuild fires. Default 6 (Sunday).",
)

register(
    "security.base_rebuild_hour_utc",
    int,
    4,
    validator=_validate_base_rebuild_hour,
    group="security",
    description="GSEC FX.64 p3a — hour (UTC) the weekly base-image rebuild "
                "fires. Default 4.",
)

# GSEC FX.123 s3 — full-disk-encryption (FDE) claim gate. When ON, a MOD-hosted
# worker (platform_pool / platform_assigned / platform_only — everything that is
# NOT the tenant's own BYOH box, see worker_tasks.py's guard) whose heartbeat
# disk_encryption block does not say status='encrypted' refuses tenant tasks.
# 'unknown'/missing counts as unencrypted (fail closed) ONLY while the setting
# is on, so an older binary that never reports FDE is unaffected while OFF.
# Default False everywhere: opt-in, like the other security.* gates.
register(
    "security.require_fde_for_customer_tasks",
    bool,
    False,
    group="security",
    description="GSEC FX.123 s3 — refuse tenant (customer) task claims on MOD-hosted "
                "workers whose heartbeat reports an unencrypted disk "
                "(hardware_specs.disk_encryption.status != 'encrypted'; missing/"
                "unknown counts as unencrypted). Off by default.",
)


def _validate_sdn_refresh_weekday(value: int) -> None:
    if not 0 <= value <= 6:
        raise ValueError(
            f"sanctions.refresh_weekday must be between 0 and 6, got {value!r}")


def _validate_sdn_refresh_hour(value: int) -> None:
    if not 0 <= value <= 23:
        raise ValueError(
            f"sanctions.refresh_hour_utc must be between 0 and 23, got {value!r}")


register(
    "sanctions.refresh_enabled",
    bool,
    False,
    group="security",
    description="GSEC FX.74 p3b — enable the weekly OFAC SDN list refresh "
                "(services/sanctions_refresh.refresh_sdn). Off by default.",
)

register(
    "sanctions.refresh_url",
    str,
    "https://www.treasury.gov/ofac/downloads/sdn.csv",
    group="security",
    description="GSEC FX.74 p3b — https URL of the OFAC SDN CSV to fetch "
                "(alt list = alt.csv in the same dir). Ignored when "
                "sanctions.refresh_drop_dir is set (air-gapped installs).",
)

register(
    "sanctions.refresh_drop_dir",
    str,
    "",
    group="security",
    description="GSEC FX.74 p3b — air-gapped mode: directory an operator "
                "drops fresh sdn.csv/alt.csv into; newer files are copied in "
                "instead of fetching sanctions.refresh_url. Empty = fetch.",
)

register(
    "sanctions.refresh_min_entries",
    int,
    1000,
    group="security",
    description="GSEC FX.74 p3b — a refreshed list must parse to at least "
                "this many entries (real SDN list ~1600) or the old file is "
                "kept. Tests may lower it.",
)

register(
    "sanctions.refresh_weekday",
    int,
    0,
    validator=_validate_sdn_refresh_weekday,
    group="security",
    description="GSEC FX.74 p3b — day of week (0=Mon..6=Sun) the weekly SDN "
                "refresh fires. Default 0 (Monday).",
)

register(
    "sanctions.refresh_hour_utc",
    int,
    5,
    validator=_validate_sdn_refresh_hour,
    group="security",
    description="GSEC FX.74 p3b — hour (UTC) the weekly SDN refresh fires. "
                "Default 5.",
)


# --- GSEC FX.77 p6b — tenant log-stream failure alert threshold -----------
def _validate_log_streams_alert_after_failures(value: int) -> None:
    if value < 1:
        raise ValueError(
            f"log_streams.alert_after_failures must be >= 1, got {value!r}")


register(
    "log_streams.alert_after_failures",
    int,
    3,
    validator=_validate_log_streams_alert_after_failures,
    group="security",
    description="GSEC FX.77 p6b — consecutive send failures on a tenant "
                "log stream (services/log_streams.py record_send_failure) "
                "before the tenant admins are emailed once per failure "
                "episode (reset on the next successful send). Default 3.",
)


# --- GSEC FX.77 s2 — platform-level log stream availability (SaaS only) ---
register(
    "log_streams.platform_stream_enabled",
    bool,
    False,
    group="security",
    description="GSEC FX.77 s2 — allow the ONE platform-level (tenant-less) "
                "log stream on a SaaS install (services/log_streams.py "
                "platform_may_use_log_streaming). On-prem installs always "
                "have it (baseline self-host SIEM capability), so this "
                "setting only gates SaaS. Default off: no platform stream "
                "until a platform admin turns it on.",
)


# --- GSEC FX.64 p2a [B] — nightly released-image re-scan -------------------
# The nightly job (services/scheduler.py id "fx64_image_rescan_daily")
# re-scans the image refs MOD actually runs (services/image_rescan over
# BuiltImageCache) with CORE/release/scan-image.sh and feeds the
# vulnerability register via CORE/release/file_findings.py. Off by
# default.
def _validate_rescan_hour(value: int) -> None:
    if not 0 <= value <= 23:
        raise ValueError(
            f"security.rescan_hour_utc must be between 0 and 23, got {value!r}")


def _validate_rescan_max_images(value: int) -> None:
    if not 1 <= value <= 5000:
        raise ValueError(
            f"security.rescan_max_images must be between 1 and 5000, "
            f"got {value!r}")


register(
    "security.rescan_enabled",
    bool,
    False,
    group="security",
    description="GSEC FX.64 p2a — enable the nightly re-scan of "
                "released/Harbor images (services/image_rescan.run_rescan "
                "via CORE/release/scan-image.sh, feeding the vulnerability "
                "register through CORE/release/file_findings.py). Off by "
                "default.",
)

register(
    "security.rescan_hour_utc",
    int,
    3,
    validator=_validate_rescan_hour,
    group="security",
    description="GSEC FX.64 p2a — hour (UTC) the nightly image re-scan "
                "fires. Default 3.",
)

register(
    "security.rescan_max_images",
    int,
    200,
    validator=_validate_rescan_max_images,
    group="security",
    description="GSEC FX.64 p2a — cap on how many image refs one nightly "
                "re-scan run will scan (most-recently-used first). Default "
                "200.",
)

register(
    "security.rescan_output_dir",
    str,
    "/app/CORE/Metadata/scans",
    group="security",
    description="GSEC FX.64 p2a — root dir under which each nightly "
                "re-scan writes its dated report sub-directory "
                "(rescan-<UTC stamp>/ holding the .trivy.json reports).",
)

# ---------------------------------------------------------------------------

# --- FX.67 part 3a (owner decision Q50=A) — customer lockbox, tenant ------
# standing pre-approval. All three are tenant-overridable (get_setting
# with tenant_id resolves the tenant row before the platform default), so
# a tenant admin opts their own tenant in; the platform default is OFF.
register(
    "lockbox.preapproval_enabled",
    bool,
    False,
    group="gsec",
    description="FX.67 p3a (Q50=A) — when True for a tenant, a NEW "
                "lockbox request against that tenant which fits inside "
                "lockbox.preapproval_max_minutes (and is read_only when "
                "lockbox.preapproval_read_only_only is set) is "
                "auto-granted at creation instead of sitting in the "
                "tenant admin's pending queue. Audited with a distinct "
                "'standing pre-approval' reason; still shown in the "
                "tenant's access log. Default False (opt-in per tenant).",
)
register(
    "lockbox.preapproval_max_minutes",
    int,
    240,
    validator=_validate_lockbox_minutes,
    group="gsec",
    description="FX.67 p3a (Q50=A) — the longest grant duration a "
                "standing pre-approval will auto-grant (240 = 4 h). A "
                "request asking for longer stays pending for the tenant "
                "admin. Must be > 0.",
)
register(
    "lockbox.preapproval_read_only_only",
    bool,
    True,
    group="gsec",
    description="FX.67 p3a (Q50=A) — when True (default), standing "
                "pre-approval only auto-grants read_only requests; a "
                "read_write request always stays pending for the tenant "
                "admin regardless of duration.",
)

# ---------------------------------------------------------------------------

# --- FX.65 p4 — on-prem PKCS#11 HSM key wrapping -------------------------
# services/kms_pkcs11.py's select_pkcs11_provider(db) reads these four
# keys. Default OFF; even when True, Pkcs11KmsProvider.__init__ itself
# refuses under utils.entitlements.is_saas() — a SaaS tenant can never
# select this provider, enabled or not. The HSM session PIN is NOT a
# setting here; it comes from Vault (see kms_pkcs11.py:_pin_from_vault),
# same as every other Vault-backed secret in this API.
register(
    "kms.pkcs11_enabled",
    bool,
    False,
    group="gsec",
    description="FX.65 p4 — on-prem only: when True, provider 'pkcs11' "
                "(services/kms_pkcs11.py) may be selected for KEK wrap/"
                "unwrap against a local HSM over PKCS#11. Default False. "
                "Always refused when utils.entitlements.is_saas() is "
                "True, regardless of this setting.",
)
register(
    "kms.pkcs11_module_path",
    str,
    "",
    group="gsec",
    description="FX.65 p4 — filesystem path to the PKCS#11 module .so "
                "(e.g. a vendor HSM driver, or SoftHSM2's "
                "libsofthsm2.so in test). Read by "
                "select_pkcs11_provider(db); empty until an admin sets it.",
)
register(
    "kms.pkcs11_token_label",
    str,
    "",
    group="gsec",
    description="FX.65 p4 — the PKCS#11 token label (as configured in "
                "the HSM / SoftHSM2's token) the pkcs11 provider opens a "
                "session against.",
)
register(
    "kms.pkcs11_key_label",
    str,
    "",
    group="gsec",
    description="FX.65 p4 — the label of the AES KEK object on the HSM "
                "token that wrap/unwrap use. The key's raw bytes never "
                "leave the HSM; only ciphertext crosses the PKCS#11 "
                "boundary.",
)

# ---------------------------------------------------------------------------
# GSEC FX.61.p2b (owner decision Q44=A) — release signature enforcement.
# ---------------------------------------------------------------------------


def _validate_signature_enforcement(value: str) -> None:
    if value not in ("warn", "enforce"):
        raise ValueError(
            f"release.signature_enforcement must be one of 'warn'|'enforce', "
            f"got {value!r}"
        )


register(
    "release.signature_enforcement",
    str,
    "warn",
    validator=_validate_signature_enforcement,
    restart_required=False,
    group="security",
    description="GSEC FX.61.p2b (Q44=A) — whether the on-prem "
                "installer/updater (CORE/startup/install.sh STEP 3.5, "
                "CORE/startup/update.sh STEP 1) aborts before loading any "
                "release image or running a migration when release "
                "signature verification (CORE/release/verify-release.sh) "
                "fails. 'warn' (default) prints a WARNING and continues; "
                "'enforce' aborts non-zero before any side effect. The "
                "EFFECTIVE value is ALWAYS 'enforce' while a gov/"
                "FedRAMP-aligned compliance profile is active, regardless "
                "of this stored value — see "
                "services.release_signing.effective_signature_enforcement, "
                "the single resolver for this setting.",
)

# ---------------------------------------------------------------------------
# GSEC FX.25 p2a (R-013) — managed-LLM (vLLM) edge vhost CIDR allowlist.
# ---------------------------------------------------------------------------
# The nginx LLM vhost (CORE/startup/nginx.conf.template, the
# ${OLLAMA_SUBDOMAIN}.${BASE_DOMAIN} server that proxies the managed-LLM
# vLLM endpoint) gates the inference path two ways: (1) an edge 401 for
# requests without an Authorization bearer (vLLM itself validates the key
# value via --api-key, FX.25 p1a/p1b), and (2) a client allowlist — the
# allow/deny include the vhost pulls in is rendered from the CIDRs in this
# setting. The entrypoint hook that writes the include
# (CORE/startup/scripts/nginx-entrypoint.sh generate_llm_vhost_acl) renders
# the DEFAULT below — keep the two in sync (the edge container cannot read
# the DB; the registration-point-trap pattern from NGINX_TRUSTED_PROXY_CIDRS).
# A changed DB value is applied by rendering services/llm_vhost_nginx.py
# over the DB value and recreating nginx in a restart window. Never env.
DEFAULT_LLM_VHOST_ALLOWED_CIDRS = [
    "100.64.0.0/10",    # tailnet — remote workers + mesh peers live here
    "10.0.0.0/8",       # RFC1918
    "172.16.0.0/12",    # RFC1918
    "192.168.0.0/16",   # RFC1918
    "127.0.0.0/8",      # loopback — localhost edge / operator checks
    "::1/128",          # IPv6 loopback
]


def _validate_llm_vhost_allowed_cidrs(value: list) -> None:
    import ipaddress
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "llm.vhost_allowed_cidrs contains a non-string entry: "
                f"{item!r}")
        try:
            net = ipaddress.ip_network(item.strip(), strict=True)
        except ValueError:
            raise ValueError(
                f"llm.vhost_allowed_cidrs contains an invalid CIDR: "
                f"{item!r}") from None
        if net.prefixlen == 0:
            raise ValueError(
                f"llm.vhost_allowed_cidrs must not contain "
                f"{item.strip()!r} (any-address) — the allowlist must "
                "stay bounded.")


register(
    "llm.vhost_allowed_cidrs",
    list,
    DEFAULT_LLM_VHOST_ALLOWED_CIDRS,
    group="llm",
    validator=_validate_llm_vhost_allowed_cidrs,
    restart_required=True,
    description="GSEC FX.25 p2a (R-013) — CIDRs allowed to reach the "
                "managed-LLM nginx vhost (deny all otherwise); rendered "
                "to the allow/deny include by the nginx entrypoint hook "
                "(CORE/startup/scripts/nginx-entrypoint.sh "
                "generate_llm_vhost_acl, default mirrored in "
                "DEFAULT_LLM_VHOST_ALLOWED_CIDRS) and "
                "services/llm_vhost_nginx.py. Default = tailnet "
                "100.64.0.0/10 + RFC1918 + loopback. Empty list = deny "
                "all (fail closed). Invalid CIDRs, host-bit CIDRs and "
                "0.0.0.0/0 are rejected by the validator. Applies on the "
                "next nginx re-render (explicit recreate — nginx is "
                "excluded from update-system.sh).",
)


# ---------------------------------------------------------------------------
# GSEC FX.81.p2a (owner decision Q64=A) — installer preflight thresholds.
# The installer (CORE/startup/install.sh, via CORE/startup/scripts/preflight.sh)
# warns when the manager host is below RECOMMENDED and BLOCKS (rc 2) when it is
# below MINIMUM. A fresh install has no DB to read these from, so preflight.sh
# carries the same defaults as bash constants; the drift guard in
# tests/test_gsec_fx81_p2a_preflight_settings.py keeps the two in sync.
# Numbers come from docs/install/system-requirements.md (pre-measurement
# estimate for a manager: 8 cores / 32 GB / 250 GB minimum, 16 cores / 64 GB /
# 500 GB recommended). No env fallback — tunable via the admin settings page.
# ---------------------------------------------------------------------------
def _validate_preflight_nonneg(value: int) -> None:
    if value < 0:
        raise ValueError(
            "install.preflight_* thresholds must be >= 0 "
            f"(got {value!r}); 0 disables that check.")


register(
    "install.preflight_min_cpu_cores",
    int,
    8,
    group="install",
    validator=_validate_preflight_nonneg,
    restart_required=False,
    description="GSEC FX.81.p2a — minimum logical CPU cores a manager host "
                "must have; the installer preflight BLOCKS below this. 0 "
                "disables the check.",
)
register(
    "install.preflight_min_ram_gb",
    int,
    32,
    group="install",
    validator=_validate_preflight_nonneg,
    restart_required=False,
    description="GSEC FX.81.p2a — minimum host RAM (GB) a manager host must "
                "have; the installer preflight BLOCKS below this. 0 "
                "disables the check.",
)
register(
    "install.preflight_min_disk_gb",
    int,
    250,
    group="install",
    validator=_validate_preflight_nonneg,
    restart_required=False,
    description="GSEC FX.81.p2a — minimum free disk (GB) at the install dir "
                "a manager host must have; the installer preflight BLOCKS "
                "below this. 0 disables the check.",
)
register(
    "install.preflight_rec_cpu_cores",
    int,
    16,
    group="install",
    validator=_validate_preflight_nonneg,
    restart_required=False,
    description="GSEC FX.81.p2a — recommended logical CPU cores for a "
                "manager host; the installer preflight WARNs below this "
                "(non-fatal). 0 disables the warning.",
)
register(
    "install.preflight_rec_ram_gb",
    int,
    64,
    group="install",
    validator=_validate_preflight_nonneg,
    restart_required=False,
    description="GSEC FX.81.p2a — recommended host RAM (GB) for a manager "
                "host; the installer preflight WARNs below this (non-fatal). "
                "0 disables the warning.",
)
register(
    "install.preflight_rec_disk_gb",
    int,
    500,
    group="install",
    validator=_validate_preflight_nonneg,
    restart_required=False,
    description="GSEC FX.81.p2a — recommended free disk (GB) at the install "
                "dir for a manager host; the installer preflight WARNs below "
                "this (non-fatal). 0 disables the warning.",
)
def _validate_security_contact_reconfirm_days(value: int) -> None:
    if value < 1:
        raise ValueError(
            "security_contact.reconfirm_days must be >= 1 day "
            f"(got {value!r}).")


register(
    "security_contact.reconfirm_days",
    int,
    182,
    group="security",
    validator=_validate_security_contact_reconfirm_days,
    restart_required=False,
    description="GSEC FX.89 p2b (owner Q75=A) — how long a tenant's "
                "security contact may go un-reconfirmed before the daily "
                "scheduler reminder fires (default 182 = 6 months). "
                "services/tenant_security_contact_service.remind_stale_"
                "security_contacts compares security_contact_confirmed_at "
                "against now - this many days; POST "
                "/tenant/security-contact/confirm resets the clock. "
                "Reminders are deduped at most once per 30 days via "
                "LifecycleEmailLog (no schema change). <1 falls back to "
                "the service constant (182).",
)


def _validate_subprocessor_notice_days(value: int) -> None:
    if value < 1:
        raise ValueError(
            "subprocessor.notice_days must be >= 1 day — the "
            "advance-notice commitment is at least the recorded period "
            "(emergency changes override per-row instead)")


register(
    "subprocessor.notice_days",
    int,
    30,
    validator=_validate_subprocessor_notice_days,
    restart_required=False,
    group="legal",
    description="GSEC FX.85 p1a — days of "
                "advance notice between announcing a subprocessor "
                "change and its effective date. "
                "services/subprocessor_changes.record_change defaults "
                "effective_at to announced_at + this many days; the "
                "default 30 sets the 'customers get at "
                "least 30 days' notice before a subprocessor is added "
                "or materially changed' commitment (row-9 note in "
                "docs/security-audit/legal-drafts/subprocessors.md). "
                "Emergency changes may override effective_at earlier "
                "per row (is_emergency=True); tenant-toggled AI "
                "providers are notice-exempt (Q70). <1 falls back to "
                "the service default (30).",
)

# ---------------------------------------------------------------------------
# GSEC FX.80 p4a (owner Q63=A, hosted cloud only) — dedicated-GPU isolation.
# ---------------------------------------------------------------------------
# Per-tenant switch: a tenant with `gpu.dedicated` True is promised its GPU
# cards are shared with NO other tenant (dedicated-tenant isolation).
# Gated by Cap.DEDICATED_GPU: setting it True is refused with 403
# unless the deployment/tenant's entitlements grant that capability, so a
# install without it can never flip the flag on. Setting it False is
# always allowed (turning a promise OFF weakens nothing).

DEDICATED_GPU_SETTING_KEY = "gpu.dedicated"


def _dedicated_gpu_allowed_for_tenant(db, tenant_id: int) -> bool:
    """May ``tenant_id`` have ``gpu.dedicated`` = True RIGHT NOW?

    Resolves the SAME way every other ``Cap.*`` gate resolves it
    (``get_entitlements`` + ``evaluate_feature`` — see
    services/log_streams.py ``tenant_may_use_log_streaming``). Hosted cloud
    only (Q63=A): self-host installs are always allowed, mirroring the
    log-streaming on-prem bypass; SaaS resolves from the tenant's plan."""
    from utils.entitlements import Cap, evaluate_feature, get_entitlements, is_saas
    if not is_saas():
        return True
    try:
        tenant = db.query(models.Tenant).filter(
            models.Tenant.id == tenant_id).first()
    except Exception:  # noqa: BLE001 — unreadable row must not wedge the write
        return False
    return evaluate_feature(
        Cap.DEDICATED_GPU, get_entitlements(db, tenant)).allowed


def check_dedicated_gpu_gate(coerced: Any, *, db, tenant_id: Optional[int]) -> None:
    """FX.80 p4a — refusing to turn a tenant's dedicated-GPU promise ON
    without the ``Cap.DEDICATED_GPU`` capability (403 + a
    platform_settings_audit trail row, written by the caller in the same
    transaction). Only the tenant-scoped True->True/False->True case is
    gated: platform scope has no tenant to promise, and False is a
    downgrade that must always be allowed (it is also the only way back
    out of a mistaken True)."""
    if not (coerced is True and tenant_id is not None):
        return
    from fastapi import HTTPException, status
    if _dedicated_gpu_allowed_for_tenant(db, tenant_id):
        return
    db.add(PlatformSettingAudit(
        key=DEDICATED_GPU_SETTING_KEY,
        scope="tenant",
        tenant_id=tenant_id,
        old_value=None,
        new_value=None,
        actor_user_id=None,
    ))
    db.flush()
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Dedicated GPU is not enabled: "
               "setting gpu.dedicated=true requires Cap.DEDICATED_GPU.",
    )


register(
    DEDICATED_GPU_SETTING_KEY,
    bool,
    False,
    group="gpu",
    description="GSEC FX.80 p4a (owner Q63=A, hosted cloud only) — "
                "dedicated-GPU isolation promise. Per-tenant "
                "switch (platform scope is the default False); setting it "
                "True requires the Cap.DEDICATED_GPU "
                "capability (403 otherwise, enforced in "
                "routers/platform_settings.py's tenant-scoped write path). "
                "A dedicated tenant's GPU tasks only run on cards with no "
                "other tenant's work, and no other tenant's task lands on a "
                "card currently running a dedicated tenant's work — see "
                "gpu_tenant_compatible in this module.",
)

# ---------------------------------------------------------------------------
# FX.165 p1a — bounded ASSIGNED VRAM hold (stale-deploy GPU leak)
# ---------------------------------------------------------------------------
# `services.gpu_reservation.cards_in_use()` counts the `gpu_assignment` of
# every ASSIGNED/CLAIMED/RUNNING task as holding VRAM. CLAIMED/RUNNING is
# honest (a worker has the task), but an ASSIGNED row that was never claimed
# — the common one being a task the claim guard just handed BACK because no
# card fit (`_assign_gpu_cards` / `_assign_stack_service_gpu_cards` keep the
# payload's `gpu_assignment` and only stamp `gpu_backoff_until`) — held its
# cards forever, so one stale row reserved the whole host and every later
# deploy bounced 20s at a time (measured: persistent-stack-deploy waited
# 4044s on worker-worker-01/02-GPU; the 2026-09-28 stack #27 incident).
# This key is the lease: an ASSIGNED row counts only while it was touched
# within N seconds of now (`updated_at`, bumped by its own onupdate=func.now()
# on the very write that stamped `gpu_assignment`). 0 is refused; the lease
# must exceed the unplaceable backoff quantum (20s) so a handed-back task
# never loses its own pin mid-backoff and blocks itself on the next claim.
def _validate_assigned_hold_lease(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("gpu.assigned_hold_lease_seconds must be a whole "
                         "number of seconds")
    if not 30 <= value <= 86400:
        raise ValueError("gpu.assigned_hold_lease_seconds must be between 30 "
                         "and 86400 seconds (above the 20s unplaceable backoff)")


register(
    "gpu.assigned_hold_lease_seconds",
    int,
    120,
    validator=_validate_assigned_hold_lease,
    group="gpu",
    description="FX.165 p1a — how long an ASSIGNED-but-unclaimed task may "
                "hold its gpu_assignment in gpu_reservation.cards_in_use(). "
                "CLAIMED/RUNNING rows always hold (a worker has them); an "
                "ASSIGNED row holds only while it was written within this "
                "many seconds, measured on api_task_queue.updated_at, so a "
                "stale ASSIGNED row (handed back unplaceable, or orphaned by "
                "a crashed scheduler) stops reserving cards and the next "
                "deploy can place. Default 120s; must stay above the 20s "
                "unplaceable backoff so a task never drops its own pin while "
                "its backoff is still live.",
)


# ---------------------------------------------------------------------------
# GSEC FX.62 p3a — bundle rolling worker upgrade tunables
# ---------------------------------------------------------------------------
# `CORE/startup/update.sh --bundle` upgrades remote workers ONE AT A TIME
# after the bundle's own API/services are healthy, reusing the existing
# drain (POST /v1/workers/{name}/pause) and one-shot self-update
# (POST /v1/workers/{name}/update) mechanisms — see worker_management.py and
# utils/worker_releases.py. These two settings are the authoritative,
# DB-backed values for that roll's per-worker timeouts (the Admin UI reads
# them the same way it reads every other platform setting); update.sh itself
# takes the same values as `--worker-drain-timeout` / `--worker-upgrade-timeout`
# CLI flags (matching defaults below) since it runs outside the API
# container and has no DB session of its own — the same split already used
# for DRAIN_TIMEOUT/--drain-timeout earlier in that script.

def _validate_positive_seconds(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be a whole number of seconds")
    if not 10 <= value <= 86400:
        raise ValueError("must be between 10 and 86400 seconds")


register(
    "workers.rolling_upgrade_drain_timeout_s",
    int,
    1800,
    validator=_validate_positive_seconds,
    group="workers",
    description="GSEC FX.62 p3a — per-worker deadline (seconds) for a "
                "bundle rolling upgrade to wait for a draining worker's "
                "in-flight tasks to finish before giving up on that worker "
                "and stopping the roll. Default 1800 (30m), mirrors the "
                "platform-wide drain timeout used by STEP 5 / --rollback. "
                "update.sh's --worker-drain-timeout flag defaults to the "
                "same value.",
)
register(
    "workers.rolling_upgrade_per_worker_timeout_s",
    int,
    900,
    validator=_validate_positive_seconds,
    group="workers",
    description="GSEC FX.62 p3a — per-worker deadline (seconds) for a "
                "bundle rolling upgrade to wait for a heartbeat reporting "
                "the new agent_version + healthy (online, update_requested "
                "cleared) after triggering POST /v1/workers/{name}/update, "
                "before stopping the roll, leaving remaining workers on "
                "the old version, and exiting non-zero (pointing at "
                "--rollback). Default 900 (15m). update.sh's "
                "--worker-upgrade-timeout flag defaults to the same value.",
)


# ---------------------------------------------------------------------------
# GSEC FX.161.p1a (owner decision Q148=A) — DLP per-tenant bulk-download /
# export anomaly detector (services/security_alert_engine.py
# RULE_EXPORT_ANOMALY, evaluated on the same fx156 tick as every other
# alert rule). Flags a tenant whose download/export activity in the
# trailing window is both >= export_anomaly_multiplier times that
# tenant's own trailing export_anomaly_lookback_days hourly-bucket median
# AND >= export_anomaly_min_count (a floor so a near-zero baseline can't
# fire on a couple of normal downloads). New tenants with no baseline
# history use the floor alone.
# ---------------------------------------------------------------------------
register(
    "dlp.export_anomaly_enabled",
    bool,
    True,
    group="dlp",
    description="GSEC FX.161.p1a — enable the per-tenant bulk-download / "
                "export anomaly detector (DLP egress). On by default.",
)
register(
    "dlp.export_anomaly_window_minutes",
    int,
    60,
    group="dlp",
    description="GSEC FX.161.p1a — length (minutes) of the trailing "
                "evaluation window whose download/export count + bytes "
                "are compared against the tenant's historical baseline.",
)
register(
    "dlp.export_anomaly_lookback_days",
    int,
    14,
    group="dlp",
    description="GSEC FX.161.p1a — trailing days of history used to "
                "build each tenant's hourly-bucket (export_anomaly_"
                "window_minutes-sized) median download/export baseline.",
)
register(
    "dlp.export_anomaly_multiplier",
    float,
    5.0,
    group="dlp",
    description="GSEC FX.161.p1a — a tenant's current-window download/"
                "export count must reach this multiple of its trailing "
                "baseline median (as well as the absolute floor) to flag.",
)
register(
    "dlp.export_anomaly_min_count",
    int,
    20,
    group="dlp",
    description="GSEC FX.161.p1a — absolute floor: a tenant's current-"
                "window download/export count below this never flags, "
                "even with zero baseline history (new tenants).",
)

# ---------------------------------------------------------------------------
# GSEC FX.161.p1b (owner decision Q148=A, DLP egress) — per-tenant
# output-destination allowlist. Rides the tenant-scoped rows of this
# registry exactly like auth.tenant_ip_allowlist above (scope='tenant',
# written via set_setting(..., tenant_id=...), read via
# get_setting(key, db, tenant_id) — tenant row > platform row > default).
# See services/dlp_output_allowlist.py for enforcement (outbound webhooks,
# run-completion webhooks, BYO-S3 storage-connection config).
# ---------------------------------------------------------------------------

register(
    "dlp.output_allowlist_enforced",
    bool,
    False,
    group="dlp",
    description="GSEC FX.161.p1b (Q148=A) — per-tenant kill switch for the "
                "output-destination allowlist. Default False = today's "
                "behaviour (no restriction). When True, "
                "dlp.output_allowlist is enforced for that tenant's "
                "outbound webhooks, run-completion webhooks and BYO-S3 "
                "storage-connection config.",
)
register(
    "dlp.output_allowlist",
    list,
    [],
    group="dlp",
    description="GSEC FX.161.p1b (Q148=A) — per-tenant list of host "
                "patterns (e.g. ['*.example.com', "
                "'s3.us-east-1.amazonaws.com']) a workflow/run is allowed "
                "to send output to. Empty = feature off (allow all, the "
                "default). Matched case-insensitively against the "
                "destination hostname (port stripped); '*'/'?' glob via "
                "fnmatch. IP literals must match an entry exactly (no "
                "wildcarding). Only enforced when "
                "dlp.output_allowlist_enforced is True for the tenant. "
                "See services/dlp_output_allowlist.check_destination.",
)

# ---------------------------------------------------------------------------
# GSEC FX.82 part 3 — SOC 2 evidence-collection job. Monthly (2nd, 06:40
# UTC) the scheduler tick (services/scheduler.py, job
# fx82p3_soc2_evidence) calls services/soc2_evidence.collect_evidence and
# writes one dated folder <soc2.evidence_dir>/<YYYY-MM>/ of small JSON
# evidence files — user-access snapshot, access-review status, security
# alert counts, backup/restore status, change log (recent migration
# files), MFA enrolment counts — each tagged with the TSC criteria it
# supports (CC6.1/CC6.2/CC6.7, CC7.1/CC7.2, CC8.1/CC8.2, A1.2), plus an
# index.json (file list + criteria + sha256). Counts/identifiers only:
# never passwords, tokens, keys or emails.
# ---------------------------------------------------------------------------
register(
    "soc2.evidence_dir",
    str,
    "CORE/Metadata/evidence/soc2",
    group="soc2",
    description="GSEC FX.82 p3 — root folder for the monthly SOC 2 "
                "evidence collection. The job writes one dated subfolder "
                "per period (e.g. CORE/Metadata/evidence/soc2/2026-09/"
                "index.json + evidence files). Relative paths resolve "
                "against the API working directory; absolute paths are "
                "used as-is.",
)
register(
    "soc2.evidence_enabled",
    bool,
    True,
    group="soc2",
    description="GSEC FX.82 p3 — enable the monthly SOC 2 "
                "evidence-collection job (services/soc2_evidence.py). "
                "On by default; when False the scheduler tick is a "
                "no-op.",
)

# ---------------------------------------------------------------------------
# GSEC FX.161.p2a (owner decision Q148=A, DLP egress) — regulated-profile
# FULL-export approval workflow (services/export_approval.py; table
# export_approval_requests in migrations/20261007zi_fx161_export_approvals.
# py). When any ACTIVE compliance profile (compliance.profiles) is listed
# in dlp.export_approval_profiles, the FX.59 full tenant export does not
# run directly: POST /v1/tenant/export creates a PENDING
# ExportApprovalRequest, and only a DIFFERENT tenant admin's approval
# (before expires_at) lets the export run — exactly once. A rejection or
# expiry raises the FX.156 alert row (security_alert_engine
# rule_key 'export_approval_denied').
# ---------------------------------------------------------------------------


def _validate_export_approval_profiles(value: list) -> None:
    # FX.162.p5 — CE carve: no compliance profile vocabulary -> no validation.
    from utils.paid_import import optional_paid
    cprof = optional_paid("services.compliance_profiles")
    if cprof is None:
        return
    bad = [v for v in value if v not in cprof.ALLOWED_PROFILES]
    if bad:
        raise ValueError(
            f"compliance profile(s) not recognized: {sorted(bad)!r} — "
            f"allowed values: {list(cprof.ALLOWED_PROFILES)}")


def _validate_export_approval_expiry_hours(value: int) -> None:
    if value < 1 or value > 24 * 30:
        raise ValueError("must be between 1 and 720 hours")


register(
    "dlp.export_approval_profiles",
    list,
    ["cmmc", "fedramp_high_il5"],
    validator=_validate_export_approval_profiles,
    group="dlp",
    description="GSEC FX.161.p2a (Q148=A) — the compliance profile ids "
                "whose activation requires a second tenant admin's "
                "approval before the FULL tenant export (FX.59) may run. "
                "Default ['cmmc', 'fedramp_high_il5'] (the gov/DoD "
                "postures); empty list = feature off (exports run "
                "directly as today). Only profiles that are actually "
                "ACTIVE (compliance.profiles) trigger the gate.",
)
register(
    "dlp.export_approval_expiry_hours",
    int,
    72,
    validator=_validate_export_approval_expiry_hours,
    group="dlp",
    description="GSEC FX.161.p2a (Q148=A) — how long (hours) an export "
                "approval request stays valid: while pending AND after "
                "approval until it is consumed by the one-time export "
                "run. Past the deadline the request is 'expired' (never "
                "authorizes an export) and the FX.156 alert is raised. "
                "Default 72 (3 days).",
)

# ---------------------------------------------------------------------------
# GSEC FX.80 p4b (Q63=A, hosted GPU isolation) — read-only model-cache mounts.
#
# The platform's model caches (/var/lib/mod/hf-cache and friends) are SHARED
# host paths. A tenant stack that binds one WRITABLE can overwrite a weight
# file another tenant then loads — cross-tenant weight poisoning. The gate
# lives in routers/persistent_stacks/_shared.py (_model_cache_ro_check): every
# tenant-owned stack mount of a known cache host path must be read-only
# (`:ro` / `read_only: true`). Downloader services are exempt via the ONE
# rule `x-mod.model_cache_writable: true` (MOD's own hf-weights-fetcher image
# counts as such automatically), and system/platform stacks (tenant None, and
# the platform tenant that owns the flat cache) are exempt so the existing
# fleet keeps working.
#
# warn    = log + append-only AuditEvent row (model_cache_ro_violation), the
#           deploy proceeds (default: turning enforce on is an operator
#           decision once the fleet is known-clean).
# enforce = the create/deploy gate returns 422 naming the offending service.
# No env vars — both are DB-backed registry settings like every other tunable.
# ---------------------------------------------------------------------------
def _validate_model_cache_ro_mode(value: str) -> None:
    if value not in ("warn", "enforce"):
        raise ValueError(
            f"model-cache RO mode must be 'warn' or 'enforce', got {value!r}")


register(
    "stacks.model_cache_ro_mode",
    str,
    "warn",
    group="stacks",
    validator=_validate_model_cache_ro_mode,
    description="GSEC FX.80 p4b — how a tenant stack's WRITABLE mount of a "
                "shared model-cache host path is handled. 'warn' (default) "
                "logs + writes an audit row and proceeds; 'enforce' refuses "
                "the create/deploy with 422. Fetcher services "
                "(x-mod.model_cache_writable: true, and MOD's own hf-weights-"
                "fetcher image) and system/platform stacks are never flagged.",
)
register(
    "stacks.model_cache_ro_paths",
    list,
    ["/var/lib/mod/hf-cache", "/var/lib/mod/models", "/var/lib/mod/model-cache"],
    group="stacks",
    description="GSEC FX.80 p4b — the shared model-cache HOST paths a tenant "
                "stack must mount read-only. A mount whose source equals one "
                "of these, or sits under one, is checked; extend the list "
                "when a fleet adds another cache root.",
)

# ---------------------------------------------------------------------------
# GSEC FX.157.p2b (Q144=A host hardening) — new-failure diff alerts.
# The weekly OFFLINE host check (CORE/startup/scripts/hardening-check.sh,
# host cron/timer 03:17 UTC Sundays) writes one evidence JSON per host into
# hardening.evidence_dir (hardening-<host>-<date>.json; in the API container
# that is the same rw-mounted tree as /metadata/hardening).
# services/hardening_diff diffs each host's latest run against its previous
# one; services/security_alert_engine.ingest_hardening (the
# fx157p2b_hardening_ingest tick, Sundays 03:40 UTC — after the host run)
# raises ONE FX.156 alert per host run listing the NEWLY failing checks
# (rule hardening_regression; a check already failing never re-alerts, the
# first-ever run is a silent baseline). Severity: high when any new failure
# is a CIS Docker Benchmark section 1/2/5 finding or a lynis warning, medium
# otherwise. The 5-minute fx156 sweep also evaluates the rule; both paths
# share the evidence state file (first-wins) so exactly one alert per run.
# ---------------------------------------------------------------------------
register(
    "hardening.regression_enabled",
    bool,
    True,
    group="hardening",
    description="GSEC FX.157.p2b — on/off for the host-hardening "
                "new-failure diff alerts (FX.156 rule "
                "hardening_regression + the fx157p2b_hardening_ingest "
                "scheduler tick). On by default; when False the tick and "
                "the rule are both no-ops.",
)
register(
    "hardening.evidence_dir",
    str,
    "CORE/Metadata/hardening",
    group="hardening",
    description="GSEC FX.157.p2b — folder the weekly offline "
                "hardening-check (hardening-check.sh) writes its "
                "hardening-<host>-<date>.json evidence files into and "
                "this service diffs. Relative paths resolve against the "
                "API working directory (same pattern as "
                "soc2.evidence_dir); the API container sees the "
                "default as /metadata/hardening. The diff state files "
                "(.hardening-state-<host>.state.json) live alongside the "
                "evidence; a dir that is read-only from the API's point "
                "of view degrades to re-arming every sweep (the engine "
                "dedupe still prevents duplicate alert rows).",
)


# ---------------------------------------------------------------------------
# GSEC FX.58 p2a (owner Q91=A — restore test WEEKLY, amends Q41 monthly) —
# the automatic restore-proof job (services/restore_test_job.py). The script
# itself (CORE/startup/restore-test.sh, held from FX.150) is unchanged: it
# restores the newest local backup set into a throwaway postgres container,
# checks it, and appends one `restore_test` line to the erasure ledger — the
# readings services/backup_ledger.read_restore_tests() and the conformance
# check `backup.restore_tested` already consume. The scheduler tick
# (services/scheduler.py, id fx58p2a_restore_test) decides due-ness from
# these two settings against the newest ledger line, so the reading can never
# age past the interval. No new env vars: the interval knob is this setting.
# ---------------------------------------------------------------------------
def _validate_restore_test_interval_days(value: int) -> None:
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"backup.restore_test_interval_days must be a whole number of "
            f"days, got {value!r}")
    if not 1 <= days <= 365:
        raise ValueError(
            "backup.restore_test_interval_days must be between 1 and 365 "
            f"days, got {days!r}")


register(
    "backup.restore_test_enabled",
    bool,
    True,
    group="gsec",
    description="GSEC FX.58 p2a (Q91=A) — on/off for the automatic restore-"
                "proof job (services/restore_test_job.sweep_once + the "
                "fx58p2a_restore_test scheduler tick). On by default: an "
                "untested backup is not a backup, so the proof runs unless "
                "an operator silences it. When false the tick skips and "
                "records nothing.",
)
register(
    "backup.restore_test_interval_days",
    int,
    7,
    validator=_validate_restore_test_interval_days,
    group="gsec",
    description="GSEC FX.58 p2a (Q91=A) — how often the restore proof runs, "
                "in days. Default 7 = WEEKLY (Q91 amends Q41's monthly "
                "cadence; the <=1h data-loss / <=4h restore targets and the "
                "35-day + monthly-for-1-year retention stand). Due when the "
                "newest restore_test line in the erasure ledger is older "
                "than this, so the job never runs twice inside one window. "
                "On-prem installs may set their own cadence; 1-365 allowed.",
)

# --- GSEC FX.58 p2b (Q41=A / Q91=A) — restore-test audit evidence + the
# conformance window -----------------------------------------------------------
# Every restore-test attempt now also writes one append-only audit-evidence
# row (AuditAction.BACKUP_RESTORE_TEST_EVIDENCE via audit.log_security_event,
# metadata-only: ok, backup id, tested backup ts, measured RTO/RPO, duration,
# the window, the attempt-row id — no content, no credentials). The
# `backup.restore_tested` conformance check judges the newest SUCCESSFUL
# reading across both p2a sources (the erasure-ledger restore_test lines and
# the SYSTEM ApiTaskQueue attempt rows) against interval_days + this grace
# window, capped by the existing backup.restore_test_max_age_days. Grace
# follows the registry's grace pattern (e.g. storage.byo_house_copy_grace_days,
# billing.payment_grace_days_monthly): a run that lands late is not yet a
# compliance gap. DB setting only, no env var.
register(
    "backup.restore_test_grace_days",
    int,
    2,
    validator=_validate_non_negative_int,
    group="gsec",
    description="GSEC FX.58 p2b (Q91=A) — grace days added to the restore-test "
                "cadence before the conformance check `backup.restore_tested` "
                "fails for a missing/aged-out successful proof. Window = "
                "backup.restore_test_interval_days + this, capped by "
                "backup.restore_test_max_age_days. Default 2 (a weekly job "
                "that lands late is not yet a gap). 0 = no grace.",
)

# ---------------------------------------------------------------------------
# GSEC FX.88 p1 (owner Q74=A) — MFA recovery codes (Keycloak).
# keycloak-init.sh's configure_recovery_codes() issues N one-time recovery
# codes at MFA setup (the SaaS convenience factor for users who lose their
# TOTP device); the gov/DoD secure-configuration profile forbids them, so
# the count setting is pinned but the init script's KC_SECURE_CONFIG_PROFILE
# gate keeps the authenticator OFF there. The codes themselves live ONLY in
# Keycloak (hashed, one-time, consumed by marking — never deleted, never
# logged); MOD stores no copies. These settings pin the knobs so the init
# script and any future API-side converge read one source of truth.
# ---------------------------------------------------------------------------
def _validate_recovery_codes_count(value: int) -> None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"auth.recovery_codes_count must be a whole number of codes, "
            f"got {value!r}")
    if not 1 <= n <= 50:
        raise ValueError(
            "auth.recovery_codes_count must be between 1 and 50 codes "
            "(the codes are single-use; more than 50 dilutes the "
            "one-time guarantee), "
            f"got {n}")


register(
    "auth.recovery_codes_enabled",
    bool,
    True,
    group="gsec",
    description="GSEC FX.88 p1 (Q74=A) — whether MFA setup issues one-time "
                "recovery codes (Keycloak 'Recovery Authentication Codes'). "
                "On by default; the gov/DoD secure-config "
                "profile (KC_SECURE_CONFIG_PROFILE at keycloak-init time, "
                "compliance.profiles at runtime) forces recovery codes OFF "
                "— resets there need two tenant admins.",
)
register(
    "auth.recovery_codes_count",
    int,
    10,
    validator=_validate_recovery_codes_count,
    group="gsec",
    description="GSEC FX.88 p1 (Q74=A) — how many one-time recovery codes "
                "MFA setup issues (Keycloak realm attribute recoveryCodesCount). "
                "Default 10 per Q74. Codes are hashed at rest, single-use "
                "(consumed by marking, never deleted), and never logged.",
)

# ---------------------------------------------------------------------------
# GSEC FX.79 p4b (owner Q62=A) — ITAR-class data markings and the US-person
# user attribute.
#
# The attribute itself is GY.V2b's (UserSetting ``export_control``
# {"us_person": true}), settable ONLY by a tenant/platform admin
# (PATCH /users/{id}/export-control) and audit-logged at the set site
# (AuditEvent US_PERSON_FLAG_SET) — no new column, no users-schema change.
# This part adds the ENFORCEMENT switch services/data_markings
# .assert_us_person_access reads: a resource whose live mark resolves to a
# MarkingLabelDef with requires_us_person (system ITAR / CUI//SP-EXPT / CUI
# plus any customer label the admin flagged) may only be read, streamed,
# exported or run over by a confirmed US person.
#
# Default True (enforce): unlike the GY.V2b export-control switch — which
# is opt-in and degrades OFF — this gate is the ITAR rule Q62 asked for and
# degrades to ENFORCED on a settings-read failure (services/data_markings
# _us_person_enforced), so an unread settings table never waves ITAR data
# past a non-US-person. Turning it OFF is an explicit operator decision for
# tenants that attest US-person status out-of-band.
# ---------------------------------------------------------------------------
register(
    "compliance.data_marking_requires_us_person",
    bool,
    True,
    group="compliance",
    description="GSEC FX.79 p4b (Q62=A) — enforce the admin-set US-person "
                "attribute against ITAR-class data markings: a workflow/asset/"
                "node_definition/named_artifact carrying a live mark whose "
                "label def has requires_us_person may only be read, streamed, "
                "exported or run over by a user the tenant admin has attested "
                "as a US person (UserSetting export_control.us_person). "
                "Default True; refusals are 403 + an append-only audit row on "
                "the marked-access record path (reason "
                "data_marking_requires_us_person). The customer attests, MOD "
                "enforces, MOD never verifies identity.",
)

# ---------------------------------------------------------------------------
# GSEC FX.78 p3 (owner Q61=A) — WebAuthn REQUIRED for admins under the
# gov/DoD profile.
#
# Q61: "FIDO2/WebAuthn security keys offered to all users and required for
# admins under the gov profile." The OFFER is GY.C3b's (auth.webauthn_enabled
# + auth.phishing_resistant_only, above); this block is the REQUIREMENT:
# services/webauthn_policy.py (gov section) converges the WEBAUTHN_REGISTER
# required action for every human ADMIN (platform admin + tenant admins =
# inviter-chain roots) while a gov/DoD compliance profile is active. The
# explicit switch below lets a non-gov on-prem tenant admin turn
# the same rule on without a compliance profile; the gov profile forces it
# regardless of this switch (so it can never be used to dodge the rule).
#
# No migration: the grace start lives in the Keycloak user attribute
# mod_webauthn_grace_started (FX.53's per-user store, no schema change) and
# state changes are append-only AuditEvent rows (WEBAUTHN_GRACE_STARTED /
# WEBAUTHN_REQUIRED_ACTION_SET) — ISO 27001, nothing hard-deleted.
# ---------------------------------------------------------------------------
def _validate_webauthn_grace_days(value: int) -> None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            "auth.webauthn_admin_grace_days must be a whole number of days, "
            f"got {value!r}")
    if not 0 <= n <= 90:
        raise ValueError(
            "auth.webauthn_admin_grace_days must be between 0 and 90 days "
            "(0 = force security-key enrollment on the very next admin login; "
            "more than 90 dilutes the gov posture Q61 asked for), "
            f"got {n}")


register(
    "auth.webauthn_required_for_admins",
    bool,
    False,
    group="auth",
    description="GSEC FX.78 p3 (Q61=A) — require a registered security key "
                "(WebAuthn credential) for every human admin: platform admins "
                "and tenant admins (inviter-chain roots). Un-enrolled admins "
                "get the WEBAUTHN_REGISTER required action queued in Keycloak "
                "after auth.webauthn_admin_grace_days, so their next login "
                "forces enrollment on the MOD-branded page. This switch is "
                "the explicit non-gov opt-in; the gov/DoD compliance profiles "
                "(cmmc / fedramp_high_il5) force the rule ON regardless of it. "
                "Machine principals (service accounts, client-credentials / "
                "direct-grant, e2e_service_principals) are never touched. "
                "Default OFF (gov profiles enforce it without this setting).",
)
register(
    "auth.webauthn_admin_grace_days",
    int,
    7,
    validator=_validate_webauthn_grace_days,
    group="auth",
    description="GSEC FX.78 p3 (Q61=A) — grace days before the WEBAUTHN_REGISTER "
                "required action is queued for an in-scope admin with no "
                "security key (same 7-day posture as FX.53's MFA grace). The "
                "grace start is stamped in the Keycloak user attribute "
                "mod_webauthn_grace_started; the Dashboard read is "
                "GET /v1/me/webauthn-enforcement. Default 7; 0 = force on the "
                "next login.",
)

# ---------------------------------------------------------------------------
# GSEC FX.64 p1a [B] — Trivy offline DB mirror stack.
#
# The stack (CORE/startup/trivy-db-mirror/: docker-compose.yml + seed.sh +
# status.sh) is a standalone 127.0.0.1-only registry:2 hosting a local copy
# of the upstream Trivy vulnerability-DB OCI repo. seed.sh syncs it while
# the box is online (or imports a bundle-shipped tarball via --from-tar,
# fully offline); scanners then pull the DB from the local registry only
# (scan-image.sh --db-repo <registry>/<repo> -> trivy --db-repository), so
# once seeded the box needs zero outbound traffic for vulnerability data.
# No secrets: the registry runs anonymous pull+push (public CVE dataset),
# so nothing goes to Vault. This block holds the pairs the API side
# (services/trivy_db_mirror.py, then the FX.64 p1b/p2a consumers) reads:
# the client ref, the upstream the seeder syncs from, the staleness bound,
# and the switch that gates handing --db-repo to any scan at all.
# ---------------------------------------------------------------------------
def _validate_trivy_db_mirror_max_staleness_hours(value: int) -> None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            "security.trivy_db_mirror_max_staleness_hours must be a whole "
            f"number of hours, got {value!r}") from None
    if not 1 <= n <= 24 * 30:
        raise ValueError(
            "security.trivy_db_mirror_max_staleness_hours must be between "
            f"1 and 720 hours (Trivy publishes the DB daily), got {n}")


register(
    "security.trivy_db_mirror_enabled",
    bool,
    False,
    group="security",
    description="GSEC FX.64 p1a — use the local Trivy DB mirror stack "
                "(CORE/startup/trivy-db-mirror/) for vulnerability scans. "
                "OFF = scanners do not receive --db-repo and run the "
                "normal way (the pre-mirror behaviour — the default, so "
                "existing installs are unchanged until the operator starts "
                "the stack AND turns this on). ON = consumers pass the "
                "registry/repo ref below to scan-image.sh --db-repo (trivy "
                "--db-repository); an unreachable/stale mirror degrades to "
                "status reporting (services/trivy_db_mirror.mirror_status), "
                "never to a scan failure.",
)
register(
    "security.trivy_db_mirror_registry",
    str,
    "127.0.0.1:5002",
    group="security",
    description="GSEC FX.64 p1a — host:port of the local Trivy DB mirror "
                "registry as seen by the scanners (the stack publishes "
                "127.0.0.1:5002 -> registry:5000; keep this equal to what "
                "seed.sh/status.sh use). Combined with "
                "security.trivy_db_mirror_repo it forms the trivy "
                "--db-repository ref (services/trivy_db_mirror.mirror_ref).",
)
register(
    "security.trivy_db_mirror_repo",
    str,
    "mod-trivy/trivy-db",
    group="security",
    description="GSEC FX.64 p1a — repo path inside the local mirror "
                "registry where seed.sh pushes the DB image "
                "(default mod-trivy/trivy-db; = seed.sh --repo). With "
                "security.trivy_db_mirror_registry it forms the scanner "
                "ref <registry>/<repo>.",
)
register(
    "security.trivy_db_mirror_upstream",
    str,
    "ghcr.io/aquasecurity/trivy-db",
    group="security",
    description="GSEC FX.64 p1a — upstream OCI repo seed.sh syncs the "
                "Trivy DB FROM while the box is online (the maintainer "
                "re-pin point for the source; air-gapped boxes use "
                "seed.sh --from-tar instead and never read this).",
)
register(
    "security.trivy_db_mirror_max_staleness_hours",
    int,
    72,
    validator=_validate_trivy_db_mirror_max_staleness_hours,
    group="security",
    description="GSEC FX.64 p1a — a mirrored Trivy DB older than this many "
                "hours is reported STALE (status.sh --max-staleness-hours "
                "and services/trivy_db_mirror.mirror_status use the same "
                "bound). Default 72: Trivy publishes the DB daily, so three "
                "missed seeds is the warning line, not an outage. The "
                "consumer decides what to do with STALE (log + audit, "
                "re-seed); the mirror itself keeps serving the last seed.",
)
# ============================================================================
# FX.78 p1a — PIV/CAC mTLS passthrough on the nginx Keycloak auth vhost.
# The setting gate for the EDGE side of the FX.78 gov login:
# ON renders the auth-vhost mTLS snippet (ssl_verify_client optional +
# the DoD PKI client-CA bundle at server scope, plus the X-SSL-Cert /
# X-SSL-Verify forward that Keycloak's nginx cert-lookup provider
# consumes) to CORE/startup/rendered/auth-vhost-mtls.conf (services/
# auth_vhost_mtls.py); compose mounts it ro at
# /etc/nginx/auth-vhost-mtls.drop.conf and scripts/nginx-entrypoint.sh
# splits it into the server-block include + the location headers.
# Default OFF = no ssl_verify_client anywhere and no X-SSL-* headers —
# default installs unchanged. Apply = re-render + recreate nginx
# (restart_required). The Keycloak side is auth.x509_enabled.
register(
    "auth.x509_edge_mtls",
    bool,
    False,
    restart_required=True,
    group="auth",
    description="FX.78 p1a — PIV/CAC mTLS passthrough on the nginx "
                "Keycloak auth vhost: ssl_verify_client optional against "
                "the DoD PKI client-CA bundle (installer drops the roots "
                "at /etc/nginx/certs/dod-ca-bundle.pem) + the X-SSL-Cert "
                "/ X-SSL-Verify forward Keycloak's nginx cert-lookup "
                "provider consumes (docs-site/docs/security/piv-cac.md; "
                "render via services/auth_vhost_mtls.apply_auth_vhost_mtls "
                "and recreate nginx to apply). Default False = no "
                "client-certificate verification at the edge (default "
                "installs unchanged). Pair with auth.x509_enabled "
                "(the Keycloak side).",
)


# ---------------------------------------------------------------------------
# GSEC FX.31 p1b — public headscale control plane: an explicit, owner-chosen
# opt-in that goes through nginx (TLS + rate limiting), NEVER a raw 0.0.0.0
# publish.
#
# FX.31 p1a pinned the SITE control plane publishes (headscale :8091, its TLS
# front :8443, the site DERP relay :8843 + :3478/udp) to 127.0.0.1-or-LAN via
# HEADSCALE_LISTEN_ADDR / HEADSCALE_TLS_LISTEN_ADDR / MOD_DERP_LISTEN_ADDR, so
# by default the control plane is invisible to the whole world. Turning THIS
# setting on renders a dedicated nginx TLS vhost (mesh-control.<BASE_DOMAIN>)
# that reverse-proxies the in-network headscale:8080 with websocket Upgrade
# headers (headscale /ts2021 needs them) under a $binary_remote_addr limit_req
# zone (10r/s burst 20 nodelay, over-limit -> 429) + the existing conn_limit
# zone. The raw publishes stay LAN/loopback in BOTH states — the setting only
# adds the rate-limited TLS front door, it never widens a host publish.
#
# Same drop-file seam as auth.x509_edge_mtls / llm.vhost_allowed_cidrs: the
# edge container cannot read the DB, so services/mesh_public_vhost_nginx.py
# renders the vhost over the DB value to CORE/startup/rendered/mesh-public-
# vhost.conf (compose mounts it ro at /etc/nginx/mesh-public-vhost.drop.conf)
# and scripts/nginx-entrypoint.sh expands it into ${MESH_PUBLIC_CONTROL_PLANE_
# VHOST} in nginx.conf.template (http{} scope — the snippet carries its own
# limit_req_zone + server block). Absent/empty drop file = OFF (default
# installs' rendered nginx.conf unchanged). Apply = re-render + recreate nginx
# (restart_required).
# ---------------------------------------------------------------------------
register(
    "mesh.public_control_plane",
    bool,
    False,
    restart_required=True,
    group="mesh",
    description="GSEC FX.31 p1b — when true the headscale control plane is "
                "reachable from the internet ONLY via the nginx TLS vhost "
                "with limit_req (mesh-control.<BASE_DOMAIN>: 443 ssl "
                "terminating at nginx with the mesh-tls headscale cert pair, "
                "proxying headscale:8080 with websocket Upgrade headers, "
                "$binary_remote_addr limit_req 10r/s burst 20 nodelay -> 429 "
                "+ conn_limit). Raw headscale/headscale-tls/DERP publishes "
                "stay LAN-or-loopback in BOTH states (FX.31 p1a), so this is "
                "the explicit owner-chosen opt-in to public control-plane "
                "reach, never a 0.0.0.0 publish. Default False = no vhost, "
                "no zone (unchanged nginx). Render via "
                "services/mesh_public_vhost_nginx.apply_mesh_public_vhost "
                "then recreate nginx to apply (restart_required).",
)
# ============================================================================
# GSEC FX.164 p1 [B] — OWASP ZAP DAST runner stack (active scanning of the
# served site + the API surface, Q151=A).
# The stack (CORE/startup/dast/: docker-compose.yml + run.sh + README) is a
# standalone pinned ZAP container on its OWN `internal: true` network — no
# outbound rules exist, so a scan can never dial a public host, a CDN, or a
# callback/OOB domain; stage is reached over the main stack's network by
# container name or over the stage localhost publish. run.sh runs zap-
# baseline.py (crawl + passive scan of the stage web root) and zap-api-
# scan.py -f openapi (the stage /v1 OpenAPI JSON), each writing a JSON
# report (-J) into the dast_reports volume; services/dast_reports.py parses
# the newest pair into normalized findings + a severity summary. The image
# is pinned by digest (the lead re-pins the <PIN_ME> placeholder before the
# stack is ever started) so nightly results are reproducible. No secrets:
# an API-surface token is an exec-time arg only, never stored. This block
# holds the pairs the API side reads — the switch, the stage target, and
# the nightly window. NO scheduler wiring yet: FX.164 p3 (vuln register +
# release gate) is what schedules the nightly run and consumes the
# summary; p2 adds Schemathesis feeding the same finding shape.
# ============================================================================
register(
    "security.dast_enabled",
    bool,
    False,
    group="security",
    description="GSEC FX.164 p1 [B] — run the OWASP ZAP DAST stack "
                "(CORE/startup/dast/) against stage. OFF (the default) = "
                "nothing scans and no report is read, so existing installs "
                "are unchanged until the operator starts the stack AND "
                "turns this on. ON = the FX.164 p3 nightly tick execs "
                "run.sh into the pinned runner and reads the JSON report "
                "pair via services/dast_reports.latest_summary; findings "
                "feed the vuln register + the release gate (p3), never a "
                "scan failure here.",
)
register(
    "security.dast_target_base_url",
    str,
    "",
    group="security",
    description="GSEC FX.164 p1 [B] — stage base URL the DAST runner scans "
                "(run.sh --target; the OpenAPI pass defaults to "
                "<base>/v1/openapi.json). Keep it LOCAL: a container name "
                "on the main stack network (http://my_fastapi_app/) or the "
                "stage localhost publish (http://127.0.0.1:8000/) — a "
                "public host name is fenced by the stack's internal "
                "network and would only ever produce an empty scan. Empty "
                "(the default) = no target configured: run.sh falls back to "
                "its own localhost default and the nightly skips the run.",
)
register(
    "security.dast_schedule_cron",
    str,
    "30 3 * * *",
    group="security",
    description="GSEC FX.164 p1 [B] — cron window for the nightly ZAP DAST "
                "run (default 03:30 UTC: off-peak, after the other nightly "
                "ticks). Stored now, WIRED IN FX.164 p3 — nothing schedules "
                "from this key yet; p3 registers the APScheduler job that "
                "execs run.sh and folds the report pair into the vuln "
                "register + release gate. Five-field cron "
                "(min hour dom mon dow).",
)

# ---------------------------------------------------------------------------
# FX.73 p4c (owner decision Q56=A) — CE-inclusion source of truth per node +
# the WorkflowBuilder palette CE badge. The stored truth is
# node_definitions.ce_included (services/ce_inclusion.py), stamped at CE seed
# time from open-core/node-seed-keeplist.yaml. This switch only controls
# whether the palette API exposes it (and so whether the WB badge renders) —
# turning it OFF hides the badge without touching any node row, which is what
# an operator needs when the badge is wrong on an install whose seed came from
# a fail-open keep-list. No migration: nothing here stores a value beyond the
# usual append-only platform_settings + audit rows.
# ---------------------------------------------------------------------------
register(
    "ce.palette_badge_enabled",
    bool,
    True,
    group="ce",
    description="GSEC FX.73 p4c (Q56=A) — expose each node's CE-inclusion flag "
                "(node_definitions.ce_included, stamped from the CE node-seed "
                "keep-list) on GET /v1/nodes so the WorkflowBuilder palette can "
                "badge 'CE'. OFF stops the enrichment (no badge in the palette); "
                "stored flags and node rows are untouched. Default ON.",
)

# ============================================================================
# FX.98 part 2a (owner decision Q84=A) — likeness-consent expiry REMINDER
# EMAILS (services/likeness_consent_reminders.py, daily scheduler job).
# Part 1 already owns the window key (likeness.consent_expiry_reminder_days,
# default 14) and both the register + the run-gate; part 2a adds the
# DELIVERY half: each tenant's admins get ONE email per day listing that
# tenant's ACTIVE consents whose expires_at sits inside the window (including
# ones that already lapsed inside the same window and were never reported).
# Both keys are read on EVERY tick, so turning the job off needs no restart.
# No new column, no migration: dedupe reuses LifecycleEmailLog's unique
# (tenant_id, email_key) pair, and every dispatched reminder writes its own
# AuditEvent row (ISO 27001 AU-2; append-only, no hard deletes).
# ============================================================================
register(
    "likeness.consent_expiry_reminders_enabled",
    bool,
    True,
    group="compliance",
    description="FX.98 p2a (Q84=A) — send the likeness-consent expiry "
                "reminder emails (services/likeness_consent_reminders."
                "run_consent_expiry_reminders, daily scheduler job) to each "
                "tenant's admins, listing the ACTIVE consents in that tenant "
                "whose expires_at is within "
                "likeness.consent_expiry_reminder_days. ON (the default) "
                "delivers the reminders; OFF stops the job entirely without "
                "touching the register, the run-gate, or any consent row. "
                "Re-read every tick — no restart needed.",
)
register(
    "likeness.consent_expiry_reminder_lead_days",
    int,
    14,
    validator=_validate_positive_int,
    group="compliance",
    description="FX.98 p2a (Q84=A) — the reminder job's lead time in days: "
                "a consent is reported when it expires within this many days "
                "(or lapsed within this many days and was never reported). "
                "Kept SEPARATE from part 1's "
                "likeness.consent_expiry_reminder_days (the register's "
                "'expiring soon' UI list) so an operator can widen the email "
                "lead time without changing what the tenant-admin page shows. "
                "Default 14, matching part 1.",
)
# ============================================================================
# GSEC FX.163 p1 (owner decision Q150=A) — segregation of duties /
# maker-checker: the tenant switch that turns the second-approver queue ON.
#
# Sensitive tenant actions (a role grant, a FULL tenant export, a BYOK
# revoke, a data-marking clear) normally run on the acting admin's own
# authority. With this switch ON for a tenant, the action is instead
# recorded as a `pending_approvals` row (models.PendingApproval,
# migrations/20261007zv_fx163_p1_pending_approvals.py) and does NOT run
# until a DIFFERENT tenant admin approves it — the DB itself refuses
# approved_by_user_id == requested_by_user_id
# (ck_pending_approvals_second_approver), so a self-approval cannot be
# written even by a raw INSERT. Default OFF (today's behaviour on normal
# installs); the gov/DoD lock profiles (cmmc / fedramp_high_il5) and the
# banking profile (glba_ffiec) force it ON — see
# services/compliance_profiles.py PROFILE_REQUIRED_SETTINGS and the
# single enforcement read services/approvals.approvals_required.
#
# Rides the tenant-scoped rows of this registry exactly like
# auth.tenant_ip_allowlist / dlp.output_allowlist_enforced above: written
# PER TENANT via set_setting(key, value, db, tenant_id=...), read via
# get_setting(key, db, tenant_id) — tenant row > platform row > default.
# No env vars (behaviour tunables live in this registry).
# ============================================================================

register(
    "approvals.require_second_approver",
    bool,
    False,
    group="approvals",
    description="GSEC FX.163 p1 (Q150=A) — segregation of duties / "
                "maker-checker for one tenant. Default False = today's "
                "behaviour (sensitive tenant actions run on the acting "
                "admin's own authority). When True for a tenant, a "
                "sensitive tenant action (role grant, full tenant export, "
                "BYOK revoke, data-marking clear) is recorded as a "
                "pending_approvals row and only runs after a DIFFERENT "
                "tenant admin approves it — the requester can never "
                "approve their own request (DB CHECK "
                "ck_pending_approvals_second_approver). Forced ON while "
                "a gov/DoD (cmmc / fedramp_high_il5) or banking "
                "(glba_ffiec) compliance profile is active. Single "
                "enforcement read: services/approvals.approvals_required"
                "(db, tenant_id).",
)

# ---------------------------------------------------------------------------
# GSEC FX.164 p2 [B] (owner decision Q151=A) — Schemathesis /v1 API fuzzing,
# the SECOND tool in the DAST stack (p1 = OWASP ZAP, the block above). Same
# stack (CORE/startup/dast/), same reports volume, same parser
# (services/dast_reports.py): schemathesis.sh runs the pinned Schemathesis
# image against the stage /v1 OpenAPI JSON and writes a JUnit XML report,
# which the parser turns into the SAME normalized finding shape with
# tool/source = "schemathesis". FX.164 p3 (vuln register + release gate)
# schedules the nightly and consumes both tools; nothing schedules here.
# The fuzzing loop is BOUNDED and RATE-LIMITED on purpose: an unbounded
# no-backoff loop storming the API is exactly the WB-poller 502-freeze
# failure mode this stack already retired, so the knobs are settings, not
# env vars, and the defaults are deliberately small.
# ---------------------------------------------------------------------------
register(
    "security.dast_schemathesis_enabled",
    bool,
    False,
    group="security",
    description="GSEC FX.164 p2 [B] (Q151=A) — run the Schemathesis /v1 API "
                "fuzzer (CORE/startup/dast/schemathesis.sh in the pinned "
                "schemathesis-runner container) against stage. OFF (the "
                "default) = nothing fuzzes and no report is read, so "
                "existing installs are unchanged until the operator starts "
                "the stack AND turns this on. ON = the FX.164 p3 nightly "
                "tick execs schemathesis.sh and folds the JUnit report into "
                "the vuln register via services/dast_reports (same finding "
                "shape as the ZAP pair, tool='schemathesis'). Independent of "
                "security.dast_enabled (ZAP): each tool has its own switch "
                "so a fleet can run one without the other.",
)
register(
    "security.dast_schemathesis_max_examples",
    int,
    20,
    validator=_validate_positive_int,
    group="security",
    description="GSEC FX.164 p2 [B] — fuzzing examples per operation "
                "schemathesis.sh passes as --max-examples (bounded work per "
                "nightly; 20 is deliberately small: a nightly must not "
                "storm the API). The nightly passes it as a CLI arg read "
                "from THIS key — no env var.",
)
register(
    "security.dast_schemathesis_rate_limit",
    int,
    2,
    validator=_validate_positive_int,
    group="security",
    description="GSEC FX.164 p2 [B] — requests per second schemathesis.sh "
                "passes as --rate-limit. 2 RPS default: the fuzzing loop "
                "must be gentler than the API's own traffic, and an "
                "un-throttled fuzz loop is what looked like the WB-poller "
                "self-DDoS 502 freeze. Whole requests/second >= 1.",
)

# ---------------------------------------------------------------------------
# GSEC FX.163 p2 [B] (owner decision Q150=A) — the DEADLINE half of the
# maker-checker queue. p1 registered the switch (approvals.require_second_
# approver, above); the SERVICE (services/approval_gate.py) needs to know
# how long a pending approval stays decidable, and that knob lives here —
# same shape as dlp.export_approval_expiry_hours (FX.161.p2a), same
# no-env-var rule. A pending row past expires_at is stamped 'expired'
# lazily on the next read/attempt (never deleted); an approved row keeps
# its deadline so a stale approval cannot run an action days later.
# ---------------------------------------------------------------------------
register(
    "approvals.pending_approval_expiry_hours",
    int,
    72,
    validator=_validate_positive_int,
    group="approvals",
    description="GSEC FX.163 p2 [B] (Q150=A) — how long (hours) a "
                "pending_approvals row stays decidable: the deadline is "
                "stamped at creation (services/approval_gate."
                "request_approval) and enforced while pending AND after "
                "approval until the action actually replays, exactly like "
                "dlp.export_approval_expiry_hours does for full-export "
                "approval requests. Past the deadline the row is stamped "
                "'expired' on the next read/attempt (lazy, ISO no-hard-"
                "delete) and the gated action can never run. Default 72. "
                "Platform scope only (the tenant switch is p1's).",
)

# --- FX.90 p3a (owner decision Q76=A) — async scan of LARGE media ----------
# Uploads at/above `malware_scan.async_above_bytes` skip the inline scan
# (a multi-GB video would pin the request handler and blow the inline cap):
# the artefact is stored, marked scan-pending on its Cog row, and an ApiTaskQueue
# SYSTEM row is queued for the streaming scan (services/malware_scan.py
# `scan_stream`). These keys drive that async path only — the inline path
# keeps `malware_scan.max_bytes`/`fail_closed` unchanged.









# ---------------------------------------------------------------------------
# GSEC FX.93 p4 [B] — hash-matching provider adapter for abuse content.
#
# "hosted-only hash-matching hook for uploads/outputs (provider adapter,
# off until owner signs up Thorn Safer/PhotoDNA)". These keys drive the
# HOOK (services/abuse_hash_match.py — not wired into the upload path
# yet): an OFF-by-default switch, the selected provider (names come
# from the services.abuse_hash_match registry), and the
# tenant-independent known-bad SHA-256 list the 'local_list' provider
# matches against — the offline/airgap substitute for an external
# hash-matching service. No env vars, no migrations; the list is a
# platform-scope JSONB list of lowercase 64-char hex hashes (max
# 10000).
# ---------------------------------------------------------------------------

def _validate_abuse_hash_match_provider(value: str) -> None:
    # Lazy import: abuse_hash_match imports platform_settings inside its
    # functions, so a module-scope import here would be a cycle.
    # CARVE-U3: the module is carve-denied — absent means there is no provider
    # registry to validate against (the paid hook is not shipped in CE), so the
    # validator is a no-op.
    from utils.paid_import import optional_paid
    _ahm = optional_paid("services.abuse_hash_match")
    if _ahm is None:
        return
    known = _ahm.provider_names()
    if value not in known:
        raise ValueError(
            f"unknown hash-match provider: {value!r} — "
            f"known: {list(known)}")


def _validate_abuse_hash_local_hashes(value: list) -> None:
    # CARVE-U3: absent (CE) -> nothing to validate against; no-op.
    from utils.paid_import import optional_paid
    _ahm = optional_paid("services.abuse_hash_match")
    if _ahm is None:
        return
    _ahm.validate_hash_list(value)  # raises ValueError on bad entries


register(
    "abuse.hash_match_enabled",
    bool,
    False,
    group="gsec",
    description="GSEC FX.93 p4 [B] (Q79=A) — hash-matching hook for "
                "uploads/outputs, OFF by default (it turns on when the "
                "owner signs up Thorn Safer/PhotoDNA): when true, "
                "services/abuse_hash_match.scan_upload_hash asks the "
                "selected provider to check a content SHA-256 and, on a "
                "match, files ONE system AbuseReport (category 'csam', "
                "no reporter user) through the existing create path. "
                "Provider errors are swallowed (logged + reported as a "
                "non-match) — the hook never raises into the caller. "
                "Hook only: not wired into the upload path yet.",
)

register(
    "abuse.hash_match_provider",
    str,
    "null",
    validator=_validate_abuse_hash_match_provider,
    group="gsec",
    description="GSEC FX.93 p4 [B] — which provider the abuse "
                "hash-matching hook uses. Allowed names are the "
                "services/abuse_hash_match registry (default 'null' = "
                "never matches; 'local_list' = the tenant-independent "
                "known-bad hash list in abuse.hash_match_local_hashes, "
                "for offline/airgap installs). Real hosted providers "
                "(Thorn Safer / PhotoDNA) register in that registry "
                "when signed up.",
)

register(
    "abuse.hash_match_local_hashes",
    list,
    [],
    validator=_validate_abuse_hash_local_hashes,
    group="gsec",
    description="GSEC FX.93 p4 [B] — tenant-independent list of "
                "known-bad SHA-256 hashes (lowercase 64-char hex, max "
                "10000 entries) the 'local_list' hash-match provider "
                "matches against. Offline/airgap installs cannot reach "
                "an external hash-matching service, so the operator "
                "curates this list from a provider's exports. Default "
                "[] — with an empty list the provider never matches.",
)


# --- FX.90 part 4a (owner decision Q76=A) — ClamAV offline signature mirror --
# The stack is CORE/startup/clamav-mirror/ (pinned nginx serving the .cvd
# bundle on 127.0.0.1:5003 = freshclam's DatabaseMirror target + a pinned
# clamd idle holder on 127.0.0.1:3310). These keys are how the API-side
# consumer (services/clamav_mirror.py) learns the mirror's ref + staleness
# bound WITHOUT shelling out, and what freshclam.sh/status.sh are invoked
# with. No env vars: the knobs are these settings.

register(
    "security.clamav_mirror_enabled",
    bool,
    False,
    group="security",
    description="FX.90 p4a (Q76=A) — a local ClamAV signature mirror exists "
                "and is used for signature refreshes. Default False: the "
                "stack is inert and nothing consults a mirror, so existing "
                "installs are unchanged until the operator starts "
                "CORE/startup/clamav-mirror AND turns this on. ON = "
                "services/clamav_mirror.mirror_ref() reports the mirror and "
                "refreshes go through it (freshclam DatabaseMirror = the "
                "local server, zero egress) instead of the upstream vendor "
                "mirrors.",
)

register(
    "security.clamav_mirror_registry",
    str,
    "127.0.0.1:5003",
    group="security",
    description="FX.90 p4a (Q76=A) — host:port of the local ClamAV "
                "signature-mirror server, i.e. the value freshclam's "
                "DatabaseMirror line and services/clamav_mirror's HTTP probe "
                "use. Keep it equal to the compose binding "
                "(127.0.0.1:5003 -> the mirror's :80). 127.0.0.1-only: "
                "scanning is a manager/build-host act, never a remote-worker "
                "service (same reasoning that retired registry:5000).",
)

register(
    "security.clamav_mirror_max_staleness_hours",
    int,
    240,
    validator=_validate_positive_int,
    group="security",
    description="FX.90 p4a (Q76=A) — a signature bundle older than this is "
                "STALE (default 240 h = 10 days, the same bound freshclam's "
                "MaxDailyDrift gets, so the mirror and freshclam agree on "
                "what 'too old' means). Consumers of "
                "services/clamav_mirror.mirror_status() decide what staleness "
                "means (audit + fall back to the upstream refresh while "
                "online, or refuse to scan at all when air-gapped). 0 "
                "disables the staleness check.",
)


# ---------------------------------------------------------------------------
# GSEC FX.80 p3 (owner Q63=A, hosted GPU isolation) — the worker's cross-tenant
# GPU VRAM scrub.
#
# The Go worker (CORE/Worker/internal/executor/gpu_tenant_scrub.go, FX.80.p2)
# zeroes a card's free VRAM between two DIFFERENT tenants' jobs so tenant B
# can never read tenant A's residue off the same card. These three keys are
# the worker's knobs, delivered on the heartbeat `tunables` map (GS.PSR.7 —
# see routers/worker_management.py._WORKER_TUNABLE_KEYS, which now carries all
# three). The API side of the same part (services/gpu_scrub_evidence.py)
# ingests the worker's scrub REPORT into the append-only audit_events trail.
#
# Hosted cloud only (Q63=A): an on-prem single-customer fleet leaves the gate
# OFF, so no card is ever scrubbed and no helper container is ever started —
# the whole path is inert. No env vars: these are registry settings.
# ---------------------------------------------------------------------------
def _validate_gpu_scrub_timeout_s(value: int) -> None:
    # The worker clamps below this itself (tenantScrubMinTimeoutS = 5), but a
    # registry write must not smuggle a 0-second scrub that every helper would
    # "time out" instantly and record as failed.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("worker.gpu_tenant_scrub_timeout_s must be a whole "
                         "number of seconds")
    if not 5 <= value <= 3600:
        raise ValueError(f"worker.gpu_tenant_scrub_timeout_s must be 5-3600 "
                         f"seconds, got {value!r}")


register(
    "worker.gpu_tenant_scrub",
    bool,
    False,
    restart_required=False,
    group="worker",
    description="GSEC FX.80 p3 (owner Q63=A, hosted cloud only) — scrub GPU "
                "VRAM (allocate + zero the card's free memory) between two "
                "DIFFERENT tenants' jobs on the same card. OFF by default: "
                "an on-prem single-customer fleet never scrubs and never "
                "starts the helper container. ON = the worker scrubs on every "
                "tenant switch it can attribute (unknown<->unknown needs no "
                "scrub) and reports each scrub on the heartbeat `gpu_scrubs` "
                "key, which the API records as an append-only audit event "
                "(services/gpu_scrub_evidence.py). Fail-closed: a scrub that "
                "cannot run is recorded ok=false and the job does not land "
                "on that card.",
)
register(
    "worker.gpu_tenant_scrub_image",
    str,
    "ghcr.io/modtim/base-cuda:12.4-python3.11",
    group="worker",
    description="GSEC FX.80 p3 — the image the scrub helper runs in. Default "
                "is the MOD Core CUDA base the node builder already hands "
                "CUDA nodes (it carries python3 and the NVIDIA driver files the "
                "container runtime mounts; no torch needed, so no new registry "
                "object is needed). A fleet running its Harbor under a "
                "different host must override this — the scrubber's helper is "
                "a CUDA driver-API program (python3 + ctypes + libcuda.so), so the "
                "image must carry python3; torch is NOT required.",
)
register(
    "worker.gpu_tenant_scrub_timeout_s",
    int,
    120,
    validator=_validate_gpu_scrub_timeout_s,
    restart_required=False,
    group="worker",
    description="GSEC FX.80 p3 — wall clock (seconds) the scrub helper is "
                "allowed before it is a FAILURE. A scrub is a memset of a "
                "few tens of GB — seconds, not minutes — so the default is "
                "tight (120) and a wedged helper fails closed rather than "
                "being waited on. 5-3600.",
)
def _validate_gpu_scrub_reserve_mib(value: int) -> None:
    # Whole MiB count, >= 0; bool is not a valid setting value (same rule as
    # the scrub timeout validator).
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("worker.gpu_tenant_scrub_reserve_mib must be a whole "
                         "number of MiB")
    if value < 0:
        raise ValueError(f"worker.gpu_tenant_scrub_reserve_mib must be >= 0, "
                         f"got {value!r}")


register(
    "worker.gpu_tenant_scrub_reserve_mib",
    int,
    1024,
    validator=_validate_gpu_scrub_reserve_mib,
    restart_required=False,
    group="worker",
    description="SCRUB-RESERVE (owner Tim 11-25) — free VRAM the GPU tenant "
                "scrub leaves untouched on each visible card, so a co-resident "
                "allocator on a shared card is never starved. The remaining "
                "free memory is zeroed in bounded 256 MiB chunks. Whole MiB "
                "count, >= 0; 0 disables the reserve (the old behaviour). "
                "Delivered with the other scrub knobs on the heartbeat "
                "`tunables` map within two heartbeats, no worker restart. "
                "With reserve >= free memory the scrub legitimately zeroes "
                "0 bytes and logs note= (no claim of a non-empty scrub).",
)
# ============================================================================
# FX.98 part 2b (owner decision Q84=A) — synthetic "no real person"
# ATTESTATION records (services/likeness_consent.record_synthetic_attestation,
# GET/POST /v1/tenant/likeness-attestations). An attestation is an
# append-only LIKENESS_RUN_GATED PermissionAuditLog row, so there is NO new
# table and NO migration; the only knob is how many audit rows the tenant
# listing / gate scan reads per call (the trail is append-only and grows
# forever, so the scan window is a tunable, not a constant).
# ============================================================================
register(
    "likeness.attestation_scan_limit",
    int,
    500,
    validator=_validate_positive_int,
    restart_required=False,
    group="compliance",
    description="FX.98 p2b (Q84=A) — how many LIKENESS_RUN_GATED audit rows "
                "one call reads when listing 'no real person' attestations "
                "or checking whether an attestation is still active "
                "(services/likeness_consent.attestation_rows). Newest first, "
                "so a revocation older than this window is not seen and the "
                "attestation reads as active again — raise it on tenants with "
                "heavy likeness traffic. Default 500, hard-capped at 5000. "
                "Read every call: no restart needed.",
)

# --- FX.95 p5a (marketplace: customer report -> delist) ----------------------
# A customer report on a catalog listing (services/abuse_reports.py,
# target_type='listing', target_id='<kind>:<id>') is normally a staff
# decision: actioning it with action_kind='listing_delisted' delists the
# listing. This knob adds the optional automatic half — enough distinct
# reporters and the listing is held out of the store (moved to pending
# review, `status='unlisted'`) until staff decide. Read by
# services/listing_review.py::autohold_if_needed at report-creation time.

register(
    "marketplace.report_autohold_threshold",
    int,
    0,
    validator=_validate_non_negative_int,
    group="marketplace",
    description="FX.95 p5a — how many DISTINCT reporters must file an open "
                "abuse report on the same marketplace listing before it is "
                "automatically held: the listing goes to pending review "
                "(review_status='staff_review') and is hidden from other "
                "tenants' catalog (status='unlisted') until staff decide. "
                "Default 0 = OFF, so nothing is auto-held and every report "
                "stays a staff decision. Counted on distinct reporter_user_id "
                "(one reporter reporting twice is still one); soft-deleted "
                "reports and anonymous submissions never count. A listing "
                "already awaiting staff review or already delisted is left "
                "alone — the auto-hold never overwrites a decision.",
)

# ============================================================================
# FX.91 p2c (owner Q77=A) — tenant-admin one-time approval of a node's
# DECLARED egress hostnames (services/node_egress_approval.py, models.
# NodeEgressApproval, migrations/20261007zz_fx91_p2c_egress_approval.py,
# POST/DELETE /v1/nodes/{id}/egress-approval). The approval record itself is
# a table, not a knob; the only knob is whether the gate ASKS at all. Default
# False keeps FX.91's shipped behaviour byte-for-byte (declared egress rides
# the dispatch allowlist with no human review), so this vertical can be
# enabled per tenant without touching the p2a/p2b manifest plumbing.
# ============================================================================
register(
    "nodes.h264_addon_enabled",
    bool,
    False,
    group="nodes",
    description="Community Edition only. Show the optional 'Video Converter "
                "(H.264)' node (add-on image video-converter-h264, Cisco "
                "OpenH264 on Debian-slim). Default False = the node is not "
                "seeded and the default Video Converter (VP9 path) is "
                "unchanged. Turn on AFTER pulling the add-on image "
                "(docker compose --profile h264 pull); the node appears at "
                "the next node refresh. Ignored by any edition that "
                "never seeds this node. OpenH264 Video Codec provided by Cisco Systems, Inc.",
)

register(
    "nodes.egress_approval_required",
    bool,
    False,
    group="nodes",
    description="FX.91 p2c — require a tenant admin's one-time approval of a "
                "node's declared egress hostnames (node_definitions.egress_"
                "domains, FX.91 p2a) before a node that DECLARES external "
                "hostnames may dispatch. Default False = unchanged behaviour "
                "(no approval asked, declared domains ride the dispatch-time "
                "allowlist exactly as shipped). When True for a tenant, a "
                "node whose declaration is non-empty and whose CURRENT "
                "declared set has not been approved by a tenant admin "
                "(models.NodeEgressApproval, one live row per tenant+node) "
                "is refused at the enqueue choke point + audited "
                "NODE_EGRESS_APPROVAL_REQUIRED. Approval is one-time: it "
                "stays valid until the node's declared set changes, and a "
                "changed set requires re-approval (the stored set is "
                "compared, never a timestamp). Enforcement read: "
                "services/node_egress_approval.gate_setting / "
                "is_approved; gate lives in routers/worker_tasks.py.",
)

# ============================================================================
# GSEC FX.59 p5a — day-60 tenant-key crypto-shred (ONE block at the END of
# this file, owner Q42=A). The daily sweep
# (services/scheduler.py::_run_tenant_key_shred, cron 04:35 UTC) walks
# tenants whose deletion was requested at least
# tenant.deletion_shred_after_days ago (``tenants.deleted_at`` — the
# soft-delete stamp the delete endpoints / cancellation-window
# finalization record write; the row itself is NEVER deleted, ISO 27001)
# and destroys their data-encryption key material (the per-tenant Vault
# transit key when the Vault-transit KEK backend is on / the stored key
# is vault-wrapped, plus the DB-side copy), making the tenant's encrypted
# data unrecoverable. Per tenant it stamps a TENANT_KEY_CRYPTO_SHRED
# AuditEvent and writes ONE append-only deletion attestation
# (services/deletion_attestation.record_attestation — identifiers +
# counts only, never key material). Idempotent: an already-attested
# tenant is skipped, and a tenant that was REACTIVATED
# (subscription.cancellation_window.status == 'reactivated') is never
# touched even if a stale deleted_at stamp survives. A legal hold blocks
# the shred (fail closed). While tenant.crypto_shred_enabled is False
# (default) the sweep runs in DRY mode: it logs and reports which
# tenants are due, destroys nothing and writes no attestation.
# ============================================================================
def _validate_tenant_deletion_shred_after_days(value: int) -> None:
    if not 30 <= value <= 365:
        raise ValueError(
            f"tenant.deletion_shred_after_days must be between 30 and 365 "
            f"days, got {value}")


register(
    "tenant.deletion_shred_after_days",
    int,
    60,
    validator=_validate_tenant_deletion_shred_after_days,
    group="privacy",
    description="GSEC FX.59 p5a — days after a tenant's deletion request "
                "(tenants.deleted_at) before the daily tenant-key "
                "crypto-shred sweep destroys the tenant's encryption "
                "key (the per-tenant Vault transit key when "
                "the Vault-transit KEK backend is on, plus the DB-side "
                "copy). 30..365, default 60 (owner Q42=A: day 60 = "
                "crypto-shred). No rows are ever deleted (ISO 27001).",
)
register(
    "tenant.crypto_shred_enabled",
    bool,
    False,
    group="privacy",
    description="GSEC FX.59 p5a — enables the daily tenant-key "
                "crypto-shred sweep. False (default) = DRY mode: the "
                "sweep logs which tenants are due and reports them but "
                "destroys nothing and writes no attestation. True = "
                "destroy the key, stamp the TENANT_KEY_CRYPTO_SHRED "
                "audit row and write the append-only deletion "
                "attestation.",
)

# ============================================================================
# FX.97 p3a (owner decision Q83=A) — signed output hash with a PER-INSTALL
# key (services/output_signature.py, models.OutputSignature). The install's
# Ed25519 private key lives in Vault KV v2 at output-signing/key and is
# NEVER a setting, a column, or an env var; the only registry entry is the
# PUBLIC key handle (mod-out-<hex>) this install minted, so the platform-
# settings audit trail records which install signed which outputs without
# any key material. p3b's verify endpoint resolves a stored key_id against
# this setting.
# ============================================================================
register(
    "output.signature_key_id",
    str,
    "",
    group="compliance",
    description="FX.97 p3a — the PUBLIC handle of this install's output-"
                "signing key (mod-out-<hex>), written once by "
                "services/output_signature.ensure_signing_key when the first "
                "workflow output is registered. The key itself is 32 random "
                "bytes held only in Vault KV v2 at output-signing/key; this "
                "row names it so a verifier (p3b) can tell a signature's "
                "origin: a stored key_id that differs from this setting was "
                "made by another install or a rotated key and reads as "
                "foreign, never as tampered. Empty = nothing signed yet on "
                "this install. Read every call: no restart needed.",
)

# ============================================================================
# FX.106 p3a — trusted-proxy ranges for real-client-IP resolution
# ============================================================================
# The app-level trust list (net.trusted_proxy_cidrs, str) predates this part
# and stays as-is so existing consumers/tests keep their seam. This is the
# LIST-typed twin: the ranges the nginx edge honours (rendered to
# set_real_ip_from) and the ranges services/client_ip.py.resolve_client_ip
# treats a direct TCP peer as a trusted proxy. resolve_client_ip uses the
# UNION of the two, so an operator who configures one side never silently
# loses the other, and a scrubbing/WAF provider's ranges can be added as
# proper list entries (dropdown-friendly, GX.S10-style UX).
# Default = exactly what CORE/startup/scripts/nginx-entrypoint.sh renders
# today (loopback + docker bridges + LAN) — the subnets our edge sits behind.
# Apply to the edge = render services/client_ip.py.apply_real_ip_block over
# this value to <start>/rendered/real-ip-drop.conf + recreate nginx
# (nginx is excluded from update-system.sh; the app side needs no restart —
# the trust cache expires in 5 s).
_PAID_TRUSTED_PROXY_RANGES = [
    "127.0.0.1/32",     # loopback — in-container callers (healthchecks)
    "::1/128",          # IPv6 loopback
    "172.16.0.0/12",    # docker bridge — the nginx reverse proxy lives here
    "192.168.0.0/16",   # LAN / WSL host subnet
    "10.0.0.0/8",       # docker classic bridge subnet
]
# CE (Community Edition) ships NO reverse proxy: the API port is published
# directly, so trusting the docker-bridge / LAN subnets would let any LAN
# client that reaches :8000 spoof its own X-Forwarded-For (the R-011 hole).
# CE therefore trusts loopback only; an operator who puts their own proxy in
# front adds that proxy's address/CIDR via Platform Settings. Fail-to-paid
# on any edition-probe error so a broken probe can never widen the trust.
DEFAULT_TRUSTED_PROXY_RANGES = _ce_unlimited(
    _PAID_TRUSTED_PROXY_RANGES,
    unlimited=["127.0.0.1/32", "::1/128"],
)


def _validate_trusted_proxy_ranges(value: list) -> None:
    import ipaddress
    if not value:
        raise ValueError(
            "network.trusted_proxy_cidrs must not be empty — an empty trust "
            "list would make every proxied request fall back to the proxy's "
            "own address; drop a range, not the list.")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "network.trusted_proxy_cidrs contains a non-string entry: "
                f"{item!r}")
        try:
            net = ipaddress.ip_network(item.strip(), strict=False)
        except ValueError:
            raise ValueError(
                f"network.trusted_proxy_cidrs contains an invalid CIDR: "
                f"{item!r}") from None
        if net.prefixlen == 0:
            raise ValueError(
                f"network.trusted_proxy_cidrs must not contain "
                f"{item.strip()!r} (any-address) — trusting every peer makes "
                "forwarded headers spoofable again (the R-011 hole).")


register(
    "network.trusted_proxy_cidrs",
    list,
    DEFAULT_TRUSTED_PROXY_RANGES,
    validator=_validate_trusted_proxy_ranges,
    group="networking",
    description="FX.106 p3a — CIDR ranges whose X-Forwarded-For / X-Real-IP / "
                "CF-Connecting-IP the API honours when resolving the REAL "
                "client IP (services/client_ip.py resolve_client_ip) and the "
                "ranges the nginx edge honours (set_real_ip_from). Default = "
                "loopback + docker bridges + LAN, i.e. the subnets our own "
                "reverse proxy sits on. A peer outside every range gets its "
                "own peer address and all forwarded headers are ignored "
                "(anti-spoof); a peer inside one yields the right-most XFF "
                "hop that is not itself trusted. Add a scrubbing/WAF "
                "provider's published ranges here (hosted cloud only) so "
                "public clients keep a real, non-spoofable IP. Union with "
                "net.trusted_proxy_cidrs (the legacy str twin). App side "
                "takes effect within 5 s; the nginx side needs "
                "apply_real_ip_block rendered to rendered/real-ip-drop.conf "
                "+ an explicit nginx recreate.",
)


# ============================================================================
# GSEC FX.115 p1a — node container lockdown flags (report-first).
#
# The Go worker (CORE/Worker/internal/executor/node_lockdown.go) applies three
# container-level lockdown knobs to every NODE container it creates, each
# behind its OWN setting and each taking off|report|enforce. The shipped
# default is "report": an untouched fleet logs one structured line per node
# container ("lockdown-report ...") naming what WOULD change and changes
# nothing, which is the report-first posture the owner asked for before any
# enforcement. Cap-drop-ALL and no-new-privileges are already what the
# executor ships today (#85), so those two mostly CONFIRM the existing
# posture; the non-root flag is the genuinely new one and the one most likely
# to break a node — FX.115 p2 (the dry-run job over the system node set) is
# what turns those report lines into a breakage list before anyone flips a
# mode to enforce.
#
# worker.node_lockdown_exempt_nodes is the interim exception surface: the API
# has no per-node exception column yet (FX.115 p3 adds one, with a recorded
# reason), so a comma-separated list of node kinds is how a node that
# genuinely needs root/caps is skipped today.
# ============================================================================
def _validate_node_lockdown_mode(value: str) -> None:
    if value not in ("off", "report", "enforce"):
        raise ValueError(
            f"node-lockdown mode must be one of 'off', 'report', "
            f"'enforce', got {value!r}"
        )


register(
    "worker.node_lockdown_nonroot",
    str,
    "report",
    validator=_validate_node_lockdown_mode,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1a — run node containers as a NON-ROOT user. "
                "'report' (default) logs per node container whether it still "
                "runs as root and changes nothing. 'enforce' sets Config.User "
                "to the reserved unprivileged uid 65534 (the same uid the "
                "worker's egress-proxy sidecar already uses, so no MOD-owned "
                "bind mount becomes unwritable); a container that already "
                "names its own user is left alone. 'off' = this layer adds "
                "nothing. Delivered on the heartbeat `tunables` map; takes "
                "effect within two heartbeats, no worker restart.",
)
register(
    "worker.node_lockdown_cap_drop_all",
    str,
    "report",
    validator=_validate_node_lockdown_mode,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1a — HostConfig.CapDrop=['ALL'] on node "
                "containers. The executor already ships this posture (#85), "
                "so 'report' (default) mostly CONFIRMS it per container and "
                "'enforce' is a no-op on the current fleet; both matter when "
                "a future executor path builds an uncapped container. Never "
                "removes an existing posture. Same heartbeat `tunables` "
                "delivery as every worker knob.",
)
register(
    "worker.node_lockdown_no_new_privs",
    str,
    "report",
    validator=_validate_node_lockdown_mode,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1a — SecurityOpt 'no-new-privileges' on node "
                "containers (a setuid/setgid inside the container may not "
                "acquire capabilities). Already the shipped posture (#85), so "
                "'report' (default) confirms it per container and 'enforce' "
                "is a no-op today; the per-cap form "
                "'no-new-privileges:true' already satisfies the flag. Same "
                "heartbeat `tunables` delivery.",
)
register(
    "worker.node_lockdown_exempt_nodes",
    str,
    "",
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1a — interim exception list: comma-separated "
                "NODE KINDS that are never locked down and are reported "
                "exempt (case-insensitive match on payload.node_kind), e.g. "
                "'hardware_benchmark,image_builder'. A node that must run as "
                "root or hold capabilities is skipped under every mode. "
                "FX.115 p3 replaces this setting with a per-node exception "
                "row carrying a recorded reason; until then this is the only "
                "exception surface.",
)

# ============================================================================
# GSEC FX.144 p2a (owner decision Q131=A) — production release approval
# (services/release_approvals.py, models.ReleaseApproval). Change control:
# agents author, the owner approves with a one-click in-app decision whose
# evidence (diff summary + CI/test results) is stored on the row. The
# EMERGENCY path ships first and the owner reviews it retroactively —
# docs/isms/09a-emergency-change-procedure.md said the window was a written
# constant ("24 hours ... there is no change_mgmt or emergency entry in the
# platform-settings registry today"); this block IS that entry now, so the
# doc's constant became a tunable per customer. Default 24 keeps the doc
# true as written (owner decision 10-07).
# ============================================================================


def _validate_fx144_review_hours(value: int, positive_min: int = 1) -> None:
    """The emergency-review window: 1..168 hours (a week at most). Anything
    longer silently stops being a review window and starts looking like an
    unattended change, so it is refused rather than accepted."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"change_mgmt.emergency_review_hours must be an int, "
                         f"got {value!r}")
    if not positive_min <= value <= 168:
        raise ValueError(
            f"change_mgmt.emergency_review_hours must be between {positive_min} "
            f"and 168 hours, got {value}")


register(
    "change_mgmt.emergency_review_hours",
    int,
    24,
    validator=_validate_fx144_review_hours,
    group="change_mgmt",
    description="FX.144 p2a — hours after an EMERGENCY production change deploys "
                "in which the owner must review it (09a 'Retroactive review': "
                "diff the deployed commits, run the CI checks on the deployed "
                "commit, record keep/replace/revert, roll back to the previous "
                "tag if the verdict is revert). Default 24; "
                "1..168. Past the deadline services/release_approvals.py stamps "
                "the row's review_status 'overdue' and the next action is "
                "rollback — an overdue emergency change is open, never "
                "'unreviewed and fine'.",
)
register(
    "change_mgmt.release_approvals_enabled",
    bool,
    True,
    group="change_mgmt",
    description="FX.144 p2a — gates the release-approval flow. True (default) = "
                "a production release records a ReleaseApproval row and waits for "
                "the owner's one-click approve (p2b's endpoint). False = the "
                "install opts out of the gate (its own release tooling ships "
                "directly); the flow is wound down, not deleted, so existing rows "
                "stay reviewable and nothing is hard-deleted (ISO 27001).",
)

# ============================================================================
# GSEC FX.106 p1a — per-plan + per-API-key API rate limits as DB platform
# settings (no env vars). Own small block at the very end; the existing
# api.rate_limit.* block above owns the master switch and the auto-block
# thresholds. Consumed by services/api_rate_limit_guard.py, which owns the
# setting-key constants and the resolution order (API-key override > plan
# limit > default_rpm) — see resolve_effective_limit there.
# ============================================================================


def _validate_fx106_plan_limits(value: dict) -> None:
    """plan_code -> positive int rpm. Shape: dict of non-empty keys, each
    value a positive integer within the guard's sane range. A plan the map
    does NOT name falls through to the plan column (where 0 = unlimited),
    so "unlimited" is expressed by omitting the plan, never by a 0 entry
    (a 0 here would be indistinguishable from a broken row)."""
    if not value:
        raise ValueError(
            "api.rate_limit.plan_limits must be a non-empty object mapping "
            f"plan code -> requests per minute (got {value!r})")
    for code, rpm in value.items():
        name = str(code).strip().lower()
        if not name:
            raise ValueError(
                "api.rate_limit.plan_limits keys must be non-empty plan "
                f"codes (got {code!r})")
        if isinstance(rpm, bool) or not isinstance(rpm, int) \
                or not (1 <= rpm <= 1000000):
            raise ValueError(
                f"api.rate_limit.plan_limits[{code!r}] must be a positive "
                f"integer between 1 and 1000000 (got {rpm!r})")


def _validate_fx106_api_key_overrides(value: dict) -> None:
    """key_prefix -> positive int rpm (same range as plan_limits)."""
    for prefix, rpm in value.items():
        name = str(prefix).strip().lower()
        if not name:
            raise ValueError(
                "api.rate_limit.api_key_overrides keys must be non-empty "
                f"key prefixes (got {prefix!r})")
        if isinstance(rpm, bool) or not isinstance(rpm, int) \
                or not (1 <= rpm <= 1000000):
            raise ValueError(
                f"api.rate_limit.api_key_overrides[{prefix!r}] must be a "
                f"positive integer between 1 and 1000000 (got {rpm!r})")


register(
    "api.rate_limit.plan_limits",
    dict,
    _FX106_RL["plan_limits"],
    validator=_validate_fx106_plan_limits,
    group=_FX106,
    description="FX.106 p1a — per-PLAN API rate limits as a registry map "
                "(plan code -> requests per minute), keyed by the plan name "
                "lower-cased. Defaults mirror the launch plans' seeded "
                "plans.api_rate_limit_per_minute (seed_launch_pricing.py) "
                "so the map and the column agree out of the box; editing "
                "this map moves the LIVE cap without touching plan rows, "
                "which is the point of the task. A plan the map does not "
                "name (custom plans) falls back to the plan "
                "column, where 0 still means unlimited. Resolution order: "
                "api.rate_limit.api_key_overrides > this map > the plan "
                "column > api.rate_limit.default_rpm.",
)
register(
    "api.rate_limit.default_rpm",
    int,
    _FX106_RL["default_rpm"],
    validator=_validate_fx106_rate_limit_int(_ce_unlimited(1, 0), 1000000,
                                             "api.rate_limit.default_rpm"),
    group=_FX106,
    description="FX.106 p1a — requests per minute applied when no plan "
                "resolves at all (no subscription, a plan the map and the "
                "column both miss). Default 120.",
)
register(
    "api.rate_limit.api_key_override_max_rpm",
    int,
    20000,
    validator=_validate_fx106_rate_limit_int(
        1, 1000000, "api.rate_limit.api_key_override_max_rpm"),
    group=_FX106,
    description="FX.106 p1a — ceiling on any single per-API-key rpm "
                "override, so one key cannot be granted an unbounded rate. "
                "Default 20000.",
)
register(
    "api.rate_limit.api_key_overrides",
    dict,
    {},
    validator=_validate_fx106_api_key_overrides,
    group=_FX106,
    description="FX.106 p1a — per-API-key rpm overrides, keyed by the API "
                "key's key_prefix. The api_keys table has no JSON metadata "
                "column of its own (capability_scope is a capability "
                "allowlist), so the per-key limit lives HERE rather than in "
                "a new column; a tenant-scoped row applies to that tenant's "
                "keys. Each value is capped by "
                "api.rate_limit.api_key_override_max_rpm.",
)


# --- FX.103 p3a (owner decision Q89=A) — node retirement SCHEDULER -------
# The runnable WINDOW (a deprecated node stays runnable before the
# retirement scheduler flips it), the platform override flag that lets an
# install deliberately accept a window below the 12-month minimum, and the
# report/dry-run switch. Read at run time by
# services/node_retirement_scheduler.run_retirement_schedule; the window
# gate (services/node_retirement_scheduler.enforce_window_minimum, called
# from the set_setting write path) is what refuses a short window unless the
# override flag is ON. Kept separate from part 1's `deprecation.*` (write
# time) and part 2's `lifecycle.*` (notice emails): the scheduler prefers
# `retirement.*` and falls back to those keys, so the three can never
# disagree about the floor.

def _validate_fx103p3a_window(value: Any) -> None:
    """retirement.min_runnable_months: 1..120 months (value shape only).
    The POLICY half of the rule — refusing anything below the 12-month
    minimum unless `retirement.allow_below_minimum_window` is ON — needs
    the session the write is committing into, so it lives in
    services/node_retirement_scheduler.enforce_window_minimum, called from
    the set_setting write path (same shape as the R-159 fleet gate above:
    a policy gate is not a value-shape check)."""
    v = int(value)
    if v < 1 or v > 120:
        raise ValueError("retirement months must be 1..120 (got "
                         f"{v})")


register(
    "retirement.min_runnable_months",
    int,
    12,
    validator=_validate_fx103p3a_window,
    group="deprecation",
    description="FX.103 p3a — months a DEPRECATED node stays RUNNABLE after "
                "`deprecated_at` before the retirement scheduler "
                "(services/node_retirement_scheduler.run_retirement_schedule, "
                "APScheduler job fx103p3a_node_retirement, daily 04:15 UTC) "
                "may flip it to 'retired'. Default 12; "
                "the sweep treats this as a HARD floor: a row's own "
                "retire_after can only move the flip later, never earlier. "
                "Refused below 12 unless retirement.allow_below_minimum_window "
                "is ON; falls back to lifecycle.min_runnable_months then "
                "deprecation.min_runnable_months when only a legacy key has "
                "a row.",
)
register(
    "retirement.allow_below_minimum_window",
    bool,
    False,
    group="deprecation",
    description="FX.103 p3a — platform override flag: while False (default) "
                "retirement.min_runnable_months cannot be set below the "
                "12-month minimum (a deprecated node must stay runnable for "
                "at least 12 months). Turning this ON "
                "is the deliberate acknowledgement that THIS install wants a "
                "shorter runway — the write is audited like every other "
                "setting change, and the sweep still clamps an unreadable "
                "value back to 12 when the flag is off.",
)
register(
    "retirement.dry_run",
    bool,
    False,
    group="deprecation",
    description="FX.103 p3a — report mode. While True the retirement sweep "
                "changes NOTHING (no status flip, no audit row) and returns "
                "the same report with the flips counted as `would_retire`, "
                "so an admin can see exactly what the next tick would do "
                "before it happens. Default False = the sweep acts.",
)

# ============================================================================
# FX.91 p2c — the SECOND knob of this vertical, in its own block at the very
# END of the file (the `nodes.egress_approval_required` switch above decides
# whether the gate ASKS at all; this one decides what it DOES when it finds
# an unapproved declaration). Default True = REPORT ONLY: the gate logs the
# unapproved-egress finding (node, tenant, declared vs approved sets) and
# hands it to the caller as a report, but never refuses the enqueue. FX.91
# p3a (gov profile: declared domains blocked unless allowlisted) is the
# half that hard-blocks, so p2c ships the decision visible + actionable
# without changing what runs. Flip False (per tenant) only once the admin
# has walked the pending list and the tenant wants the refusal enforced.
# ============================================================================
register(
    "nodes.egress_approval_report_only",
    bool,
    True,
    group="nodes",
    description="FX.91 p2c — while nodes.egress_approval_required is on, "
                "REPORT an unapproved declared-egress finding (log line + "
                "the gate's report) instead of refusing the dispatch. "
                "Default True: nothing is blocked by p2c, so enabling the "
                "vertical cannot change what runs; FX.91 p3a owns the gov-"
                "profile hard-block. Set False to make the gate refuse the "
                "enqueue (status 'egress_approval_required') once the tenant "
                "admin has approved the pending list.",
)

# ============================================================================
# GSEC FX.115 p1b — the SECOND half of the node container lockdown layer,
# built on p1a (the block above at worker.node_lockdown_nonroot / _cap_drop_all
# / _no_new_privs / _exempt_nodes). Same contract, same consumer
# (CORE/Worker/internal/executor/node_lockdown.go, applyNodeLockdown, one
# structured "lockdown-report" line per node container): every mode knob is
# its OWN platform setting taking off|report|enforce with the shipped default
# "report" (log what WOULD change, change nothing), and the same
# worker.node_lockdown_exempt_nodes allow-list exempts a node under EVERY
# p1b knob too.
#
# Three knobs:
#   worker.node_lockdown_seccomp          -> the seccomp profile attached to
#                                            node containers, named by
#                                            worker.node_lockdown_seccomp_profile
#   worker.node_lockdown_readonly_rootfs  -> HostConfig.ReadonlyRootfs +
#                                            writable scratch tmpfs for the
#                                            paths tools still need (/tmp,
#                                            /var/tmp, /run, /root, /home)
#   worker.node_lockdown_limits_{pids,memory,cpu} -> resource-limit FLOORS
#                                            whose values come from the
#                                            matching _cap settings
#
# The never-regress rules this layer obeys (why the defaults are what they are
# and why "enforce" is a no-op on the current fleet):
#   * the worker already attaches its OWN embedded node seccomp profile
#     (sandbox.go seccomp-node.json), which is STRICTER than Docker's built-in
#     default, so a container carrying a seccomp= option is covered and this
#     layer never re-writes or removes it; profile "docker" therefore writes
#     nothing (it would be a step DOWN onto a weaker filter), and only the
#     named "mod-node" profile is actionable on a container that has none;
#   * the hardened sandbox already sets ReadonlyRootfs + those scratch tmpfs
#     mounts, so "report" mostly CONFIRMS that posture; a path that is a bind
#     MOUNT DESTINATION (/data, the checkpoint volume, ...) is never turned
#     into tmpfs — tmpfs would shadow the bind and hide the tenant's I/O —
#     and /dev/shm is never tmpfs (it is ShmSize, sized by the executor and
#     by ephemeral.go);
#   * a limit floor is written ONLY where the container is UNLIMITED, never
#     on top of a cap the payload/executor already chose (rewriting it in
#     either direction changes the node's declared budget; going BELOW it
#     would OOM or starve a node that asked for more). So a _cap of 0 means
#     "this layer requests no floor", and an existing cap is reported as
#     already-hardened. GPU nodes are unaffected: device passthrough lives in
#     Resources.Devices/DeviceRequests, which this layer never reads.
# ============================================================================
def _validate_node_lockdown_profile(value: str) -> None:
    """The seccomp profile NAME. 'docker' (the default) = Docker's built-in
    default profile, which is what an un-seccomp'd container already gets, so
    it is a request for "no change"; 'mod-node' = the worker's embedded node
    profile, the only name this binary can actually render. The list is open
    on the left so a future release can register a stricter named profile
    before the worker learns to render it — the worker's own getter ignores a
    name it cannot attach, so an unknown value reports Missing but never
    Needed."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"seccomp profile name must be a non-blank string, got {value!r}"
        )
    if value != value.strip():
        raise ValueError(
            f"seccomp profile name must not be padded, got {value!r}"
        )


def _make_node_lockdown_cap_validator(name: str, maximum: int):
    """Factory for the three limit-value validators. 0 = "this layer requests
    no floor for that resource" (the shipped default: an untouched fleet
    changes no container's budget). The ceiling keeps one metadata value from
    claiming the host's RAM/CPU the way an uncapped node declaration could
    (ephemeral.go's cap for /dev/shm and /tmp is the same idea)."""
    def _validate_node_lockdown_cap(value) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"{name} must be an int (0 = no floor), got {value!r}"
            )
        if value < 0 or value > maximum:
            raise ValueError(
                f"{name} must be between 0 (no floor) and {maximum}, got {value}"
            )
    return _validate_node_lockdown_cap

register(
    "worker.node_lockdown_seccomp",
    str,
    "report",
    validator=_validate_node_lockdown_mode,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1b — the seccomp filter attached to node "
                "containers. 'report' (default) logs per container whether it "
                "carries one and changes nothing. 'enforce' attaches the "
                "profile named by worker.node_lockdown_seccomp_profile, but "
                "ONLY on a container that has none and only when that name is "
                "one this worker can render ('mod-node' = the embedded node "
                "profile) — a container already carrying a seccomp= option is "
                "covered and is never re-written or removed. 'off' = this "
                "layer adds nothing. The worker ships its embedded profile "
                "today (sandbox.go), so 'report' mostly CONFIRMS it.",
)
register(
    "worker.node_lockdown_seccomp_profile",
    str,
    "docker",
    validator=_validate_node_lockdown_profile,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1b — WHICH seccomp profile the seccomp knob "
                "asks for. 'docker' (default) = Docker's built-in default, "
                "which is exactly what an un-seccomp'd container already gets, "
                "so it writes nothing (attaching the worker's stricter profile "
                "under it would be an unrequested step UP, and writing a "
                "default onto a container that has the embedded one would be "
                "a step DOWN onto a weaker filter). 'mod-node' = the worker's "
                "embedded node profile (CORE/Worker seccomp-node.json), the "
                "only name this binary can render, so it is the only value "
                "that can change a container. Unknown names report Missing but "
                "never Needed. Read together with worker.node_lockdown_seccomp.",
)
register(
    "worker.node_lockdown_readonly_rootfs",
    str,
    "report",
    validator=_validate_node_lockdown_mode,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1b — read-only container rootfs + writable "
                "scratch tmpfs. 'report' (default) logs whether the image "
                "layers are still writable and names the scratch paths enforce "
                "WOULD add, changing nothing. 'enforce' sets "
                "HostConfig.ReadonlyRootfs and puts the scratch paths "
                "(/tmp, /var/tmp, /run, /root, /home — the same set and sizes "
                "the hardened sandbox uses) on tmpfs, so a compromised node "
                "process cannot write /usr, /bin, /etc. Bind mounts keep "
                "their own rw/ro config and IGNORE the read-only rootfs, so "
                "the tenant's /data I/O volume still works; a scratch path "
                "that is a bind MOUNT DESTINATION is never tmpfs'd (tmpfs "
                "would shadow the bind and hide tenant data), an existing "
                "tmpfs entry is left alone (the hardened sandbox's and the "
                "node's own ephemeral sizes win), and /dev/shm is never tmpfs "
                "(it is ShmSize). 'off' = this layer adds nothing, not even "
                "scratch. Already read-only = already hardened.",
)
register(
    "worker.node_lockdown_limits_pids",
    str,
    "report",
    validator=_validate_node_lockdown_mode,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1b — the process (PID) floor for node "
                "containers: a fork-bomb guard. 'report' (default) logs "
                "whether the container is PID-unlimited and changes nothing. "
                "'enforce' writes worker.node_lockdown_limits_pids_cap ONLY "
                "where the container has no cap (unset, 0 or -1 = unlimited) "
                "— an existing cap is the node's own budget and is never "
                "rewritten, tighter or looser. 'off' = nothing. The executor "
                "already ships a 4096 default, so 'report' mostly CONFIRMS it.",
)
register(
    "worker.node_lockdown_limits_pids_cap",
    int,
    0,
    validator=_make_node_lockdown_cap_validator(
        "worker.node_lockdown_limits_pids_cap", 65536),
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1b — the PID floor worker.node_lockdown_limits_pids "
                "enforces. 0 (default) = 'this layer requests no floor', so "
                "even enforce writes nothing: the layer refuses to invent a "
                "budget for a node. 1..65536 = the floor, applied only where "
                "the container is PID-unlimited. Delivered on the heartbeat "
                "tunables map like every worker knob.",
)
register(
    "worker.node_lockdown_limits_memory",
    str,
    "report",
    validator=_validate_node_lockdown_mode,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1b — the memory floor for node containers. "
                "'report' (default) logs whether the container is RAM-unlimited "
                "and changes nothing. 'enforce' writes "
                "worker.node_lockdown_limits_memory_cap_mb ONLY where the "
                "container is uncapped — the payload's own MemoryLimitMB and "
                "the worker's host-RAM default already win and are never "
                "rewritten, because going BELOW a cap the node asked for would "
                "OOM it. 'off' = nothing.",
)
register(
    "worker.node_lockdown_limits_memory_cap_mb",
    int,
    0,
    validator=_make_node_lockdown_cap_validator(
        "worker.node_lockdown_limits_memory_cap_mb", 1048576),
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1b — the memory floor in MB that "
                "worker.node_lockdown_limits_memory enforces. 0 (default) = "
                "no floor requested. 1..1048576 (1 TiB) = the floor, written "
                "only on an uncapped container. RAM-backed, so the ceiling "
                "stops one metadata value from claiming the host.",
)
register(
    "worker.node_lockdown_limits_cpu",
    str,
    "report",
    validator=_validate_node_lockdown_mode,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1b — the CPU floor for node containers. "
                "'report' (default) logs whether the container is CPU-unlimited "
                "and changes nothing. 'enforce' writes "
                "worker.node_lockdown_limits_cpu_cap_cores ONLY where the "
                "container has no NanoCPUs quota — the payload's own CPULimit "
                "and the worker's host-core-count default already win and are "
                "never rewritten. 'off' = nothing.",
)
register(
    "worker.node_lockdown_limits_cpu_cap_cores",
    int,
    0,
    validator=_make_node_lockdown_cap_validator(
        "worker.node_lockdown_limits_cpu_cap_cores", 4096),
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p1b — the CPU floor in WHOLE cores that "
                "worker.node_lockdown_limits_cpu enforces (written as NanoCPUs "
                "= cores x 1e9). 0 (default) = no floor requested. 1..4096 = "
                "the floor, applied only where the container has no CPU quota.",
)

# ============================================================================
# GSEC FX.144 p3a — the OTHER two knobs of the emergency-change vertical, in
# their own block at the very END of the file (p2a's block above owns the
# review WINDOW; these decide how often the overdue sweep runs and whether an
# overdue review makes a noise). The sweep is APScheduler job
# ``fx144p3a_emergency_review`` (services/scheduler.py) calling
# services/release_approvals.sweep_overdue_reviews: it stamps emergency rows
# past ``review_due_at`` as review_status 'overdue' (+ the append-only
# SECURITY_INCIDENT_CREATED audit row) and raises ONE platform-level
# SecurityAlert per row through the FX.156 engine's dedupe table, so the
# part-2 email/digest + ack path sees each overdue review exactly once.
# 09 + 09a (owner decision 10-07): the window itself is 24h and is p2a's
# setting — nothing here changes it.
# ============================================================================


def _validate_fx144p3a_sweep_minutes(value: int) -> None:
    """Sweep cadence: 1..720 minutes (12h at most), or 0 = 'disabled'. A
    cadence coarser than the window would make 'within 24h' unenforceable,
    so anything over 12h is refused rather than silently accepted."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("change_mgmt.emergency_review_sweep_minutes must be "
                         f"an int, got {value!r}")
    if value < 0 or value > 720:
        raise ValueError("change_mgmt.emergency_review_sweep_minutes must be "
                         "0 (disabled) or between 1 and 720 minutes, got "
                         f"{value}")


register(
    "change_mgmt.emergency_review_sweep_minutes",
    int,
    15,
    validator=_validate_fx144p3a_sweep_minutes,
    group="change_mgmt",
    description="FX.144 p3a — how often (minutes) the overdue-emergency-review "
                "sweep runs: it stamps emergency rows past review_due_at as "
                "'overdue' and raises one SecurityAlert per row. Default 15. "
                "0 = disabled (the job stays registered but no-ops, so "
                "switching needs no restart; the LAZY stamp on the next read "
                "of the approval page still applies, so disabling the sweep "
                "never hides an overdue row — it only stops the alerting "
                "heartbeat). 1..720 otherwise.",
)
register(
    "change_mgmt.emergency_review_alert_enabled",
    bool,
    True,
    group="change_mgmt",
    description="FX.144 p3a — while True (default) an overdue emergency review "
                "raises ONE platform-level SecurityAlert per row "
                "(rule_key 'emergency_review_overdue', subject "
                "'release_approval:<id>') through the FX.156 engine, so the "
                "part-2 email/digest + ack path and the security-alert page "
                "see it exactly once. False = the stamp + the audit row still "
                "happen (an overdue review is still OPEN, never 'unreviewed "
                "and fine') but no alert is raised — the alerting half is "
                "turned off, the tracking half is not.",
)

# ============================================================================
# FX.97 p4 — C2PA Content Credentials for image outputs
# ============================================================================
register(
    "output.c2pa_images_enabled",
    bool,
    False,
    group="compliance",
    description="FX.97 p4/p6a (owner Q83=A) — embed a C2PA manifest (PNG caBX / "
                "JPEG APP11) in EXPORTED image outputs (the stored blob is never "
                "rewritten; the labelled copy is served from a short-lived temp "
                "object). FX.97 p6a is the policy layer services/c2pa_policy.py: "
                "with NOTHING stored, AI-generated outputs (the FX.97 p1 report "
                "has an 'ai' step) are labelled ON and non-AI outputs OFF; a "
                "stored row overrides that — a platform row sets the install-wide "
                "answer, a TENANT row (this key per tenant, like auth.tenant_ip_"
                "allowlist / approvals.require_second_approver: PATCH "
                "/platform-settings/{key} with tenant_id) overrides the AI "
                "default either way. Resolution: tenant row > platform row > "
                "AI/non-AI default > this registry default (False = undecided "
                "runs stay byte-identical).",
)

# ============================================================================
# FX.144 p3b — auto change log (git + deploy records) for a release
# ============================================================================
def _validate_fx144p3b_max_commits(value: int) -> None:
    """Commit cap per change-log read: 1..500. 0 or negative would make the
    log structurally empty, which is refused rather than silently accepted."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("change_mgmt.changelog_max_commits must be an int, "
                         f"got {value!r}")
    if value < 1 or value > 500:
        raise ValueError("change_mgmt.changelog_max_commits must be between "
                         "1 and 500, got " + str(value))


def _validate_fx144p3b_git_timeout(value: int) -> None:
    """git subprocess ceiling: 1..60 seconds. Anything longer would let one
    change-log read hold a request open for a minute-plus."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("change_mgmt.changelog_git_timeout_seconds must be "
                         f"an int, got {value!r}")
    if value < 1 or value > 60:
        raise ValueError("change_mgmt.changelog_git_timeout_seconds must be "
                         "between 1 and 60, got " + str(value))


register(
    "change_mgmt.changelog_repo_path",
    str,
    "",
    restart_required=False,
    group="change_mgmt",
    description="FX.144 p3b — absolute path of the repo the auto change log "
                "reads commit history from (``git log --no-pager`` against "
                "``from_ref..to_ref``, read-only). Empty (the default) means "
                "the install opted out — the API container has no source tree "
                "mounted — and the change log answers with an empty commit "
                "list plus ``source_unavailable: true`` and the reason, never "
                "a guess at some other tree and never an error. The deploy "
                "records half (release_approvals) is always joined regardless.",
)
register(
    "change_mgmt.changelog_max_commits",
    int,
    100,
    validator=_validate_fx144p3b_max_commits,
    group="change_mgmt",
    description="FX.144 p3b — how many commits one change-log read may return "
                "(git is asked for one more so the response can say whether "
                "the list was truncated). Bounds the read, not the release: "
                "a release with more commits than this still reports "
                "``truncated: true``. Default 100, 1..500.",
)
register(
    "change_mgmt.changelog_git_timeout_seconds",
    int,
    10,
    validator=_validate_fx144p3b_git_timeout,
    group="change_mgmt",
    description="FX.144 p3b — the subprocess ceiling (seconds) for the "
                "``git log`` the change log shells out to. A timeout is a "
                "clean 'cannot read the source' answer (empty commits + "
                "``source_unavailable``), never a hung request and never a "
                "500. Default 10, 1..60.",
)

# ============================================================================
# FX.91 p3a — gov/DoD profile: declared node egress BLOCKED unless allowlisted
# (the last clause of the FX.91 contract, owner Q77=A). Built on p2a
# (node_definitions.egress_domains, services/node_egress_domains.py) and p2c
# (services/node_egress_approval.py: approval_state/pending/approve/revoke +
# the report-only dispatch gate). Two knobs, both read by
# services/node_egress_approval (gov_egress_decision is the pure rule;
# gov_gate_reason is the DB half the enqueue calls): the ALLOWLIST itself
# (which external hostnames the install permits at all) and whether the
# posture is ON at all (a gov compliance profile FORCES it on regardless —
# same forcing as release_signing.effective_signature_enforcement).
# ============================================================================
def _validate_egress_allowlist_domains(value: list) -> None:
    """Strict validator: the p2a manifest grammar, verbatim.

    Plain hostnames + leading ``*.`` wildcards only (2+ labels behind the
    wildcard), no bare ``*``, no scheme/port/path/IP/IDN, max 50 entries —
    so the allowlist and the node declarations are ONE vocabulary and a
    malformed list is refused at the settings write path (400) instead of
    silently loosening the gov block.
    """
    from services import node_egress_domains
    try:
        node_egress_domains.normalise_egress_domains(value)
    except node_egress_domains.EgressDomainError as exc:
        raise ValueError(
            f"nodes.egress_allowlist_domains rejected ({exc.code}): "
            f"{exc.message}"
        ) from None


register(
    "nodes.egress_allowlist_domains",
    list,
    [],
    group="nodes",
    validator=_validate_egress_allowlist_domains,
    description="FX.91 p3a — external hostnames this install PERMITS a node "
                "to declare (the gov-profile allowlist; tenant row > platform "
                "row). Same grammar as node_definitions.egress_domains (FX.91 "
                "p2a): plain hostnames and leading '*.' wildcards only, no "
                "bare '*', no scheme/port/path/IP, max 50 entries. Default [] "
                "= NOTHING allowlisted, so under an active gov/DoD compliance "
                "profile (cmmc / fedramp_high_il5) every declaring node is "
                "blocked until the admin allowlists exactly what the install "
                "vouches for. Coverage is scope containment: a declared "
                "'*.cdn.example.com' needs an allowlist entry at least as "
                "wide ('*.cdn.example.com' or '*.cdn.example.com' parent), an "
                "exact entry never covers a declared wildcard, and a wildcard "
                "never covers the apex (same rule as the Worker egress proxy).",
)
register(
    "nodes.egress_gov_allowlist_enforced",
    bool,
    False,
    group="nodes",
    description="FX.91 p3a — while True, declared node egress outside "
                "nodes.egress_allowlist_domains is a HARD block at the enqueue "
                "choke point (status 'egress_gov_blocked'), regardless of "
                "nodes.egress_approval_report_only and of any tenant approval. "
                "Default False = inert (non-gov installs keep p2c's shipped "
                "behaviour byte-for-byte). An ACTIVE gov/DoD compliance "
                "profile (cmmc / fedramp_high_il5) FORCES this True — the "
                "profile's posture wins, so no tenant/platform write can "
                "switch the block off on a gov install; this knob exists so a "
                "non-gov install can adopt the same posture ahead of time.",
)

# ============================================================================
# --- FX.103 p3b (owner decision Q89=A) — 6-MONTH NOTICE EMAILS -------------
# The delivery half of the retirement vertical: tenants whose workflows use a
# deprecated node get ONE notice email while the node's retirement date
# (the SAME date services/node_retirement_scheduler.run_retirement_schedule
# schedules the flip on) is still `retirement.notice_lead_months` (default 6)
# months or less away. Deduped per (node, tenant, notice stage) through
# LifecycleEmailLog's unique (tenant_id, email_key) pair — no new table, no
# migration. Read at run time by services/node_deprecation_notices.run_deprecation_notice_emails
# (APScheduler job fx103p3b_deprecation_notices, daily 04:20 UTC, i.e. after
# part 2's 04:05 and part 3a's 04:15 tick). Kept separate from part 2's
# `lifecycle.notice_months` (that one drives the part-2 per-NODE one-shot
# stamp + the retire-refusal floor): this key is the per-TENANT email lead
# time and is the knob the owner asked for.

def _validate_fx103p3b_notice_lead(value: Any) -> None:
    """retirement.notice_lead_months: 1..120 months (value shape only)."""
    v = int(value)
    if v < 1 or v > 120:
        raise ValueError("retirement notice lead must be 1..120 months "
                         "(got " + str(v) + ")")


register(
    "retirement.notice_emails_enabled",
    bool,
    True,
    group="deprecation",
    description="FX.103 p3b — while True (default) the daily notice tick "
                "(services/node_deprecation_notices.run_deprecation_notice_emails) "
                "sends each tenant's admins ONE email per deprecated node that "
                "tenant's workflows use, once that node's retirement date is "
                "within retirement.notice_lead_months. Turning it OFF stops "
                "the emails only — the p3a retirement flip and the part-2 "
                "dispatch run guard are unaffected, so a tenant that silences "
                "the warning still loses the node on the date. Re-read every "
                "tick: no restart needed.",
)
register(
    "retirement.notice_lead_months",
    int,
    6,
    validator=_validate_fx103p3b_notice_lead,
    group="deprecation",
    description="FX.103 p3b — months of NOTICE before a deprecated node is "
                "retired (default 6). The notice fires when the "
                "node's effective retirement date — max(deprecated_at + "
                "retirement.min_runnable_months, retire_after), the same date "
                "the p3a scheduler flips on — is at or before now + this many "
                "months and still in the future. Widening it warns tenants "
                "earlier; narrowing it delays the warning. Exactly-once per "
                "(node, tenant, day) via LifecycleEmailLog, so a rerun never "
                "double-notices.",
)
register(
    "retirement.notice_emails_dry_run",
    bool,
    False,
    group="deprecation",
    description="FX.103 p3b — report mode for the notice tick. While True the "
                "tick changes NOTHING (no send, no LifecycleEmailLog dedupe "
                "row, no audit row) and returns the notices it WOULD send as "
                "`would_send` + `noticed_nodes`, so an admin can see the fan-"
                "out before the next tick actually mails anyone.",
)

# ============================================================================
# GSEC FX.115 p4 — hosted gVisor (runsc) as a RUNTIME option for node
# containers. p1a/p1b/p3 hardened the container's OWN posture (user, caps,
# seccomp, rootfs, resource floors); this is the orthogonal knob: WHICH OCI
# runtime the daemon runs the node container under. gVisor (runsc) sits
# between the container and the kernel, so it is the answer to "kernel-side
# escape" that no HostConfig flag can give — but it is also a HOST capability
# (the runtime must be registered in the docker daemon), and this repo NEVER
# installs it and NEVER restarts docker. So the rule is report-first AND
# fail-soft: the worker asks the daemon (`docker info` Runtimes) whether runsc
# exists, and when it does not the task still runs under runc with the
# lockdown report saying `gvisor_unavailable` — a missing runtime is an audit
# finding, never a failed run.
#
#   worker.node_runtime_gvisor      -> off | report | enforce (default OFF,
#                                      unlike the p1 knobs: switching a whole
#                                      fleet onto a different runtime is a
#                                      bigger step than confirming a posture,
#                                      and nothing is even installable until
#                                      the owner says so)
#   worker.node_runtime_gvisor_gpu  -> bool, default False: gVisor's GPU path
#                                      (nvproxy) is limited and breaks most
#                                      CUDA tooling, so a GPU-requesting node
#                                      container is NEVER switched to runsc
#                                      unless this sub-flag is explicitly on;
#                                      either way the report says `gpu_skipped`
#
# Exemptions reuse p3's list: a node kind exempt for flag `gvisor`
# (worker.node_lockdown_exempt_list entry `kind:gvisor`) runs under runc.
# ============================================================================
register(
    "worker.node_runtime_gvisor",
    str,
    "off",
    validator=_validate_node_lockdown_mode,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p4 — which OCI runtime node containers run "
                "under: 'off' (default — the daemon's default runtime, runc, "
                "unchanged), 'report' (log per container what WOULD change, "
                "change nothing), 'enforce' (HostConfig.Runtime='runsc' ONLY "
                "when the docker daemon actually reports a runsc runtime). "
                "This layer never installs runsc and never restarts docker: "
                "runsc absent is a fallback to runc plus "
                "'gvisor_unavailable' in the lockdown report, never a failed "
                "task. Same heartbeat tunables delivery; a node kind exempt "
                "for flag 'gvisor' in the p3 exception list stays on runc.",
)
register(
    "worker.node_runtime_gvisor_gpu",
    bool,
    False,
    restart_required=False,
    group="worker",
    description="GSEC FX.115 p4 — whether a node container that REQUESTS a "
                "GPU (DeviceRequests capabilities [[gpu]] or raw --device "
                "mounts) may also be switched to runsc. Default False: gVisor's "
                "GPU support is the nvproxy path and most CUDA/ROCm tooling "
                "fails under it, so GPU nodes are left on runc and the report "
                "records 'gpu_skipped'. Set True only on a host whose runsc "
                "build is known to serve that node's GPU path.",
)


# KEEP-WARM-SLICE-1 (owner Tim 11-25) — opt-in keep-warm containers. Two
# switches, BOTH default so nothing changes until someone opts in (plus the
# node-definition flag custom_metadata.warm_policy). No licence gate and no
# quantity cap: same keys and defaults in CE and paid.
_WARM_POOL_HELP = (
    "Warm containers keep a node container running between calls to skip "
    "start-up time. A warm container is never shared across tenants or image "
    "versions, its data folders are wiped and verified empty between calls, "
    "and each call runs as a new process; process memory cannot be shared "
    "between calls in exec mode. Off by default. "
)
register(
    "worker.warm_pool_enabled",
    bool,
    False,
    restart_required=False,
    group="worker",
    description=_WARM_POOL_HELP + "This is the TENANT switch (written per "
                "tenant, scope='tenant', by a tenant admin): a task is only "
                "run warm when the node's own warm_policy flag is on, this "
                "tenant's switch is on, and the platform allowance "
                "worker.warm_pool_allowed is on. One tenant's switch never "
                "affects another tenant.",
)
register(
    "worker.warm_pool_allowed",
    bool,
    True,
    restart_required=False,
    group="worker",
    description=_WARM_POOL_HELP + "This is the PLATFORM kill switch "
                "(platform admin, platform scope only): set it to false to "
                "stop every warm container for every tenant at once. Default "
                "true, which only allows the opt-in: nothing runs warm until "
                "a node flags warm_policy AND a tenant admin turns "
                "worker.warm_pool_enabled on. Delivered to workers by "
                "heartbeat.",
)


def _validate_fx97p5_video_max(value: Any) -> None:
    """output.c2pa_video_max_bytes: 0 (no ceiling) or 1 byte..1 TiB."""
    v = int(value)
    if v < 0 or v > 1099511627776:
        raise ValueError("c2pa video ceiling must be 0 (no ceiling) or "
                         "1 byte..1 TiB (got " + str(v) + ")")


register(
    "output.c2pa_video_max_bytes",
    int,
    2147483648,
    validator=_validate_fx97p5_video_max,
    group="compliance",
    description="FX.97 p5 — the largest MP4/MOV (bytes) an EXPORT may carry "
                "C2PA Content Credentials in. The clip is streamed (never "
                "buffered) and the manifest box is APPENDED, so this is not "
                "a memory limit but a cap on the extra read+write the "
                "download does; a clip above it is served byte-identical "
                "with the reason recorded (skipped, never failed). 0 means "
                "no ceiling. Tenant rows override the platform row, like "
                "every other key here.",
)

# ============================================================================
# --- FX.91 p3b (owner decision Q77=A) — BLOCKED-EGRESS AUDIT DEDUPE ---------
# The recording half of the egress vertical: every blocked (or report-only
# WOULD-be) node-egress attempt at the dispatch line writes ONE append-only
# PermissionAuditLog row (AuditAction.NODE_EGRESS_BLOCKED) and ships it to
# the SIEM immediately (services/node_egress_approval.record_blocked_egress).
# A run that retries the same blocked node must not flood that trail, so the
# emit dedupes on (tenant, gate, node) inside THIS window — the only tunable
# p3b needs, so it lives here like every other knob in the vertical
# (nodes.egress_approval_required / report_only / the gov allowlist pair).
# No new env var, no new table: the dedupe read is an indexed EXISTS over the
# audit table itself.

def _validate_fx91p3b_block_window(value: Any) -> None:
    """security.egress_block_audit_window_seconds: 1..86400 seconds (shape)."""
    v = int(value)
    if v < 1 or v > 86400:
        raise ValueError("egress-block audit dedupe window must be 1..86400 "
                         "seconds (got " + str(v) + ")")


register(
    "security.egress_block_audit_window_seconds",
    int,
    300,
    validator=_validate_fx91p3b_block_window,
    group="security",
    description="FX.91 p3b — how long a blocked node-egress attempt SUPPRESSES "
                "the next audit row for the same (tenant, gate, node). A run "
                "that retries a blocked node inside this window emits ONE row, "
                "not one per retry (the flood the audit trail must avoid); "
                "widen it to make a persistent block quieter, narrow it to "
                "see every attempt. The row itself is append-only either way "
                "— nothing is ever deleted, and the SIEM re-ship of an "
                "existing row is keyed by its id.",
)

# ---------------------------------------------------------------------------
# FX.101 part 2a (owner decision Q87=A) — tenant require-SSO.
# Same GS.PSR.2 tenant-scoped mechanism as auth.tenant_ip_allowlist above:
# auth.require_sso is written PER TENANT (scope='tenant') and read by
# services/sso_requirement.py through get_setting(key, db, tenant_id).
# Default false = password login works exactly as before (the off posture).
# The verified-domain half of the predicate is NOT stored here — it reuses
# the home-realm-discovery resolver (sso.domain_routing), which already
# joins tenant_verified_domains (state='verified') to an ACTIVE
# tenant_idp_configs row and yields the IdP alias the refusal hands back.
# ---------------------------------------------------------------------------

register(
    "auth.sso_requirement_enabled",
    bool,
    True,
    group="gsec",
    description="FX.101 part 2a (Q87=A) — global kill switch for the "
                "tenant require-SSO login gate. When false, no tenant's "
                "auth.require_sso row is enforced for ANY tenant (password "
                "login works everywhere). Default true. Reads resolve "
                "tenant row > platform row > default, so a tenant may "
                "carry its own override. Read on every interactive login "
                "by services/sso_requirement.sso_required_for.",
)

register(
    "auth.require_sso",
    bool,
    False,
    group="gsec",
    description="FX.101 part 2a (Q87=A) — per-tenant require-SSO (written "
                "PER TENANT, scope='tenant'). TRUE = users whose email "
                "domain is a VERIFIED domain of THIS tenant may sign in "
                "ONLY through this tenant's SSO IdP: password / direct-grant "
                "and device-grant logins are refused 403 with code "
                "'sso_required' plus the IdP hint (kc_idp_hint alias + "
                "broker URL) the UI redirects to. IdP-brokered (SSO) logins "
                "and API keys (machine credentials) are unaffected. FALSE / "
                "absent = password login works (default). Enforced only when "
                "this tenant actually has an ACTIVE IdP bound to that "
                "verified domain — turning the flag on before the IdP is "
                "active never strands users. Platform admins are exempt "
                "(they sign in on the platform realm). Read by "
                "services/sso_requirement.sso_required_for; the break-glass "
                "tenant-admin exemption (FX.101 p2b) short-circuits that "
                "same predicate.",
)

# =============================================================================
# GSEC FX.95 p5c (owner decision Q81=A) — MOD-built-only marketplace posture.
# A gov/DoD profile (services.node_egress_approval.GOV_PROFILES: cmmc +
# fedramp_high_il5) FORCES it: only nodes/listings whose publisher is MOD
# itself (the platform tenant, or the NULL = system/official marker) may be
# installed or run. fedramp_moderate does not force it, so a tenant on a
# non-forcing profile adopts the same posture only by setting this key — the
# same shape as nodes.egress_gov_allowlist_enforced in FX.91 p3a. Read by
# services/gov_mod_built_only.mod_built_only_active; refusals are audited
# there (append-only PermissionAuditLog, no new enum member).
# =============================================================================
register(
    "marketplace.mod_built_only_enforced",
    bool,
    False,
    group="marketplace",
    description="GSEC FX.95 p5c — opt a NON-forcing compliance profile into the "
                "MOD-built-only marketplace posture: third-party (non-MOD-built) "
                "workflow/stack/node-package listings cannot be installed or "
                "cloned, and runs of third-party nodes are refused at the "
                "dispatch line. Already-installed third-party items are NOT "
                "deleted — only the run is refused. An active gov profile "
                "(cmmc / fedramp_high_il5) forces this regardless of the key; "
                "the key is how a non-gov or fedramp_moderate install opts in.",
)

# ---------------------------------------------------------------------------
# FX.101 part 2b (owner decision Q87=A) — break-glass tenant admin.
# Same GS.PSR.2 tenant-scoped mechanism as auth.require_sso above (scope
# 'tenant', group 'gsec'): auth.require_sso_break_glass_user_ids is written
# PER TENANT by PUT /v1/tenant/security/require-sso (routers/tenant/
# security.py -> services/sso_break_glass.py) and read by
# services/sso_requirement.sso_required_for, which short-circuits to
# "password login allowed" for a listed user. Empty/absent = nobody is
# exempt (the default posture: require_sso locks every verified-domain
# user to the IdP). The value-shape validator below caps the list at 2
# ids per tenant; the "each id is a tenant admin of THAT tenant" rule is a
# cross-table check a settings validator cannot see (validators get the
# value only, never the db/tenant — same split as the FX.103 p3a / GY.C3b
# gates, which live in set_setting's own guards), so it is enforced in the
# service write path (services/sso_break_glass.py) that owns this key.
# Every break-glass password sign-in emits a high-severity security audit
# row (AuditAction.AUTH_LOGIN_SUCCESS + metadata.reason
# 'auth.require_sso.break_glass') from that same short-circuit.
# ---------------------------------------------------------------------------

_FX101P2B_BREAK_GLASS_MAX = 2


def _validate_fx101p2b_break_glass_ids(value: Any) -> None:
    """A list of user ids, at most 2 per tenant. Raises ValueError so
    set_setting maps the write to an error the router turns into a 400."""
    if value is None:
        return
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise ValueError(
            "auth.require_sso_break_glass_user_ids must be a list of user ids"
        )
    if len(value) > _FX101P2B_BREAK_GLASS_MAX:
        raise ValueError(
            "auth.require_sso_break_glass_user_ids holds at most "
            f"{_FX101P2B_BREAK_GLASS_MAX} ids per tenant"
        )
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int) or entry <= 0:
            raise ValueError(
                "auth.require_sso_break_glass_user_ids entries must be "
                f"positive user ids ({entry!r})"
            )


register(
    "auth.require_sso_break_glass_user_ids",
    list,
    [],
    validator=_validate_fx101p2b_break_glass_ids,
    group="gsec",
    description="FX.101 part 2b (Q87=A) — per-tenant break-glass list (written "
                "PER TENANT, scope='tenant'). Up to 2 user ids of THIS tenant "
                "that keep password sign-in even while that tenant's "
                "auth.require_sso is ON — the designated tenant admin who "
                "signs in when the tenant's SSO IdP is down. Each id must be a "
                "tenant admin (inviter-chain root) OF THAT tenant, enforced by "
                "the write path in services/sso_break_glass.py. EMPTY/absent = "
                "nobody is exempt (default). A listed user's password login is "
                "audited at high severity (AUTH_LOGIN_SUCCESS + metadata.reason "
                "'auth.require_sso.break_glass'); the exemption is read by "
                "services/sso_requirement.sso_required_for, which returns "
                "'no requirement' for them.",
)

# ============================================================================
# GSEC FX.164 p4 [B] (owner Q151=A) — the DAST INGEST SCHEDULE.
# p1/p2 (the CORE/startup/dast/ runners) WRITE the report pair into the
# dast_reports volume; p3 (services/dast_ingest) turns findings into register
# rows + the release gate. p4 closes the loop on the API side: a scheduler
# job (id fx164p4_dast_ingest) parses the newest pair every
# security.dast_ingest_interval_minutes and ingests it, reading the volume
# through the read-only mount added to the api service in
# CORE/startup/docker-compose.yml (applies at the restart window — the live
# API container only sees the mount once it is next recreated).
# security.dast_ingest_enabled stays OFF by default: until that mount is
# live the API cannot see the reports, so the honest default is off and an
# owner turns it on deliberately once the stack is running. The reports dir
# itself is p3's `vuln.dast_reports_dir` (default /zap/wrk = exactly where
# the runners mount the volume), so no new path key is invented here.
# ============================================================================


def _validate_fx164p4_ingest_minutes(value: Any) -> None:
    """Ingest cadence in minutes: 1..1440. 0/negative is refused rather
    than silently meaning 'never' — the enable switch
    (security.dast_ingest_enabled) is the documented off button, so a
    cadence must be a real cadence. Coarser than daily is refused: the
    runners are a NIGHTLY act, so an ingest pass that waits longer than a
    day would drop a night's evidence out of the register."""
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            "security.dast_ingest_interval_minutes must be an integer "
            f"number of minutes (got {value!r})"
        )
    if value < 1 or value > 1440:
        raise ValueError(
            "security.dast_ingest_interval_minutes must be between 1 and "
            f"1440 minutes (got {value!r})"
        )


register(
    "security.dast_ingest_enabled",
    bool,
    False,
    group="security",
    description="GSEC FX.164 p4 [B] — run the DAST INGEST tick "
                "(scheduler job fx164p4_dast_ingest): parse the newest "
                "report pair in the dast_reports volume via "
                "services/dast_reports and merge the findings into the "
                "vuln register (services/dast_ingest.run_ingest_tick). "
                "OFF (the default) = the tick logs a no-op and reads "
                "nothing, because until the dast_reports volume is "
                "mounted read-only into my_fastapi_app "
                "(CORE/startup/docker-compose.yml, restart window) the "
                "API cannot see the reports at all. ON = the register "
                "tracks the nightly scans, so a new High blocks the "
                "release bundle (vuln.dast_release_gate, p3). Re-read "
                "EVERY tick: turning this on/off needs no restart.",
)
register(
    "security.dast_ingest_interval_minutes",
    int,
    60,
    validator=_validate_fx164p4_ingest_minutes,
    group="security",
    description="GSEC FX.164 p4 [B] — how often (minutes) the DAST ingest "
                "tick reads the report pair and updates the vuln register "
                "(scheduler job fx164p4_dast_ingest). 60 default: the "
                "runners are nightly, so hourly ingest is comfortably "
                "finer than the producer and costs one dir scan + one "
                "register rewrite per pass. 1..1440 only — the enable "
                "switch is the off button, so a cadence must be a real "
                "cadence. Read at scheduler start, so a change lands on "
                "the next restart window (the tick itself re-reads the "
                "enable switch every pass).",
)

# ---------------------------------------------------------------------------
# GSEC FX.112 p3a — tenant-admin alert rules + thresholds (services/
# tenant_alert_rules.py, built on the FX.156 engine). A tenant admin may
# set each tunable rule's threshold ONLY inside these platform-set
# min/max bounds (PUT /v1/tenant/security/alert-rules/{rule_key} -> 422
# out of bounds); enable/disable is always available. Defaults mirrored
# in services/tenant_alert_rules.py so a read hiccup degrades to sane
# bounds rather than dead-ending the write.
# ---------------------------------------------------------------------------
register(
    "security_alerts.tenant_signin_threshold_min",
    int,
    3,
    group="security",
    description="GSEC FX.112 p3a — lowest per-tenant threshold a "
                "tenant admin may set on the signin_failures rule "
                "(failed sign-ins per user/IP over the sliding window). "
                "Below this, the PUT returns 422. Default 3.",
)
register(
    "security_alerts.tenant_signin_threshold_max",
    int,
    100,
    group="security",
    description="GSEC FX.112 p3a — highest per-tenant threshold a "
                "tenant admin may set on the signin_failures rule. "
                "Above this, the PUT returns 422. Default 100.",
)
register(
    "security_alerts.tenant_download_threshold_min",
    int,
    5,
    group="security",
    description="GSEC FX.112 p3a — lowest per-tenant threshold a "
                "tenant admin may set on the unusual_downloads rule "
                "(bulk downloads/exports per user over the trailing "
                "hour). Below this, the PUT returns 422. Default 5.",
)
register(
    "security_alerts.tenant_download_threshold_max",
    int,
    1000,
    group="security",
    description="GSEC FX.112 p3a — highest per-tenant threshold a "
                "tenant admin may set on the unusual_downloads rule. "
                "Above this, the PUT returns 422. Default 1000.",
)
register(
    "security_alerts.tenant_export_min_count_min",
    int,
    5,
    group="security",
    description="GSEC FX.112 p3a — lowest per-tenant floor a tenant "
                "admin may set on the export_anomaly rule (DLP "
                "absolute min count, dlp.export_anomaly_min_count). "
                "Below this, the PUT returns 422. Default 5.",
)
register(
    "security_alerts.tenant_export_min_count_max",
    int,
    1000,
    group="security",
    description="GSEC FX.112 p3a — highest per-tenant floor a tenant "
                "admin may set on the export_anomaly rule. Above "
                "this, the PUT returns 422. Default 1000.",
)
# ---------------------------------------------------------------------------
# GSEC FX.119 p2a (owner decision Q106=A) — the on-call PAGING CHANNEL.
# Monitoring alerts and owner-on-call Sev-1 tickets page the on-call person's
# phone through a self-hosted push app (ntfy, deployed as a MOD persistent
# stack — deploy/ntfy/docker-compose.ntfy.example.yml) that also works
# offline on-prem. These are the API-side knobs only; the rota (oncall.rota),
# the 15-minute ack window and the escalation target are FX.119 p3a/p3b and
# are NOT read by anything yet. No env vars: everything here is DB-backed.
# ---------------------------------------------------------------------------
register(
    "oncall.paging_enabled",
    bool,
    False,
    group="gsec",
    description="GSEC FX.119 p2a — master switch for the on-call paging "
                "channel. Default False: the push stack is not deployed "
                "yet, and MOD never implies a human is reachable. When "
                "false, send_page() makes no HTTP call at all.",
)
register(
    "oncall.push_base_url",
    str,
    "",
    group="gsec",
    description="GSEC FX.119 p2a — origin of the self-hosted ntfy push "
                "stack (mesh/LAN origin, never ntfy.sh), e.g. "
                "http://203.0.113.42:8080. Empty = no channel configured.",
)
register(
    "oncall.push_topic",
    str,
    "mod-oncall-platform",
    group="gsec",
    description="GSEC FX.119 p2a — ntfy topic the API publishes platform-"
                "level pages to. Must sit under the stack's "
                "mod-oncall-* ACL prefix (publisher write-only).",
)
register(
    "oncall.push_timeout_seconds",
    int,
    5,
    group="gsec",
    description="GSEC FX.119 p2a — HTTP timeout for one page POST. An "
                "unreachable push stack must never raise into the caller, "
                "so this bounds the stall. Default 5.",
)
register(
    "oncall.push_priority_map",
    dict,
    {"sev1": 10, "sev2": 8, "sev3": 5, "sev4": 4, "sev5": 3},
    group="gsec",
    description="GSEC FX.119 p2a — page severity -> ntfy priority (1 low "
                ".. 10 urgent). Unknown severities fall back to "
                "oncall.push_priority_default. Keys are compared "
                "case-insensitively with '-' stripped (SEV-1 == sev1).",
)
register(
    "oncall.push_priority_default",
    int,
    5,
    group="gsec",
    description="GSEC FX.119 p2a — ntfy priority used when the page's "
                "severity is not in oncall.push_priority_map. Default 5.",
)
register(
    "oncall.push_dedupe_window_seconds",
    int,
    600,
    group="gsec",
    description="GSEC FX.119 p2a — identical dedupe_key values inside "
                "this window send ONE page (the second is reported "
                "deduped and never re-POSTed). 0 disables dedupe. "
                "Default 600 (10 min).",
)
register(
    "oncall.push_vault_path",
    str,
    "infrastructure/ntfy",
    group="gsec",
    description="GSEC FX.119 p2a — Vault KV path holding the ntfy "
                "publisher token (field `publisher_token`). Only the "
                "PATH is a setting; the token itself is never stored "
                "here, in another setting, or in a log.",
)

# ---------------------------------------------------------------------------
# GSEC FX.120 p2a (owner decision Q107=A) — JIT production admin elevation.
# There is NO standing admin on production infrastructure (host shell,
# database admin, Vault admin, Docker): an operator files an elevation
# REQUEST (models.PrivilegedElevationRequest, services/privileged_elevation.py)
# that carries a reason, one of the four scopes, and a TTL that AUTO-REVOKES.
# These three platform settings are the tunables the request path reads —
# platform-wide (MOD platform staff acting on MOD's own production infra),
# NOT tenant-scoped, and deliberately NOT env vars.
# ---------------------------------------------------------------------------


def _validate_elevation_ttl(name: str, minimum: int, maximum: int):
    """Bounds for the elevation TTL knobs. The DB ceiling (10080 = 1 week,
    ck_privileged_elevation_requests_ttl) is the hard one; the platform
    setting is the policy one an admin can tighten (or loosen, but never
    past the DB ceiling) without a deploy."""
    def _validate_elevation_ttl_value(value) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"{name} must be an int (minutes), got {value!r}"
            )
        if value < minimum or value > maximum:
            raise ValueError(
                f"{name} must be between {minimum} and {maximum} minutes, "
                f"got {value}"
            )
    return _validate_elevation_ttl_value


register(
    "security.elevation_default_ttl_minutes",
    int,
    120,
    validator=_validate_elevation_ttl(
        "security.elevation_default_ttl_minutes", 1, 10080),
    restart_required=False,
    group="security",
    description="GSEC FX.120 p2a (Q107=A) — TTL (minutes) an elevation "
                "request gets when the operator names none. Default 120 (2h).",
)
register(
    "security.elevation_max_ttl_minutes",
    int,
    480,
    validator=_validate_elevation_ttl(
        "security.elevation_max_ttl_minutes", 1, 10080),
    restart_required=False,
    group="security",
    description="GSEC FX.120 p2a (Q107=A) — longest elevation window an "
                "operator may request (minutes). Requests over this are "
                "refused (422-equivalent ValueError); the DB keeps its own "
                "hard ceiling at 10080 (1 week). Default 480 (8h).",
)
register(
    "security.elevation_solo_mode",
    bool,
    False,
    restart_required=False,
    group="security",
    description="GSEC FX.120 p2a (Q107=A) — second-person approval for "
                "production elevation. False (default) = a DIFFERENT "
                "platform admin must approve every request. True = solo "
                "mode: the operator self-approves, and the fact is alerted "
                "AND audited (self_approved=True on the row + the audit "
                "row) so the weekly FX.71 access report always names it. "
                "Default False.",
)

# ---------------------------------------------------------------------------
# GSEC FX.120 p2b/p2c (owner decision Q107=A) — the APPROVAL POLICY + the
# auto-revoke SCHEDULE for JIT production-admin elevation. p2a shipped the
# data layer (models.PrivilegedElevationRequest + services/privileged_elevation.py);
# p2b decides WHO may approve (a second platform admin by default; solo mode
# self-approves and ALERTS + audits it), stamps the pending approval-window
# deadline, and requires a reason on a denial; p2c registers the APScheduler
# sweep (services/scheduler.py:_fx120_elevation_expiry_tick) that auto-revokes
# a pending row past its approval window and an approved/active row past
# expires_at. No env vars: every knob is a platform setting.
# ---------------------------------------------------------------------------


def _validate_fx120_approval_window_minutes(name: str, minimum: int,
                                            maximum: int):
    """Bounds for the pending approval window. A window of 0 would mean
    'a request lapses the instant it is filed' (nobody could ever approve
    it), so the floor is 1 minute; the ceiling keeps a pending request from
    squatting on the ledger for a season."""
    def _validate_fx120_approval_window_value(value) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"{name} must be an int (minutes), got {value!r}")
        if value < minimum or value > maximum:
            raise ValueError(
                f"{name} must be between {minimum} and {maximum} minutes, "
                f"got {value}")
    return _validate_fx120_approval_window_value


register(
    "security.elevation_approval_window_minutes",
    int,
    30,
    validator=_validate_fx120_approval_window_minutes(
        "security.elevation_approval_window_minutes", 1, 10080),
    restart_required=False,
    group="security",
    description="GSEC FX.120 p2b (Q107=A) — how long an elevation request "
                "may sit 'pending' before the auto-revoke sweep expires it "
                "unanswered (minutes). create_request stamps "
                "expires_at = created_at + this, so an abandoned request "
                "can never be approved later (a request nobody decided is "
                "NOT a grant). Default 30; 1..10080 (the DB's 1-week "
                "ceiling).",
)


def _validate_fx120_expiry_check_seconds(value) -> None:
    """Bounds for the auto-revoke sweep cadence (SECONDS). 0 = the sweep is
    OFF (the job stays registered and no-ops, so turning it on/off needs no
    restart); the ceiling is one day so the sweep can never drift past a
    season. The floor above 0 is 1s — a cadence must be a real cadence."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            "security.elevation_expiry_check_seconds must be an int "
            f"(seconds), got {value!r}")
    if value < 0 or value > 86400:
        raise ValueError(
            "security.elevation_expiry_check_seconds must be 0 (disabled) "
            "or between 1 and 86400 seconds, got "
            f"{value}")


register(
    "security.elevation_expiry_check_seconds",
    int,
    60,
    validator=_validate_fx120_expiry_check_seconds,
    restart_required=False,
    group="security",
    description="GSEC FX.120 p2c (Q107=A) — cadence (seconds) of the "
                "elevation auto-revoke sweep job (fx120_elevation_expiry): "
                "it expires a 'pending' row past its approval window and an "
                "'approved'/'active' row past expires_at, each expiry "
                "audited. 0 = the sweep is OFF (the job stays registered but "
                "no-ops, so switching needs no restart — but remember an "
                "OFF sweep means NO auto-revoke, which is the whole guarantee, so it is a deliberate "
                "operator choice, not a convenience. Default 60; 0..86400.",
)

register(
    "security.siem_alerts_enabled",
    bool,
    True,
    restart_required=False,
    group="security",
    description="GSEC FX.112 p5b — ship SecurityAlert lifecycle events "
                "(raised / acked) to the FX.77 SIEM export stream "
                "(services/security_alert_siem.py): the platform audit row "
                "(carried by services/siem_export.py) plus the tenant "
                "log-stream AuditEvent (tenant-scoped; platform-level alerts "
                "reach no tenant stream). Default True; the ship is "
                "best-effort and never breaks alert creation / ack.",
)

# ---------------------------------------------------------------------------
# GSEC FX.119 p3b (owner decision Q106=A) — the on-call ROTA. p2a shipped the
# paging CHANNEL (whoever the API publishes to); p3b makes that "whoever" a
# SCHEDULE: a weekly rota of named people, each with their own ntfy topic, a
# tier (primary/secondary), and the days/hours they are on call. A page goes
# to the person who is actually on call right now; with no rota configured it
# falls back to oncall.owner_topic (the escalation target = the owner/manager
# who is always reachable) and says so ONCE per window in the audit trail
# (ONCALL_NO_ROTA — a compliance trail that shows the gap, not a flood).
# No env vars: the rota is DB-backed so an admin edits it in the admin UI
# without a deploy.
# ---------------------------------------------------------------------------


def _validate_oncall_rota(value) -> None:
    """Shape check for the rota list. Entries are dicts with a name, a topic
    (the ntfy topic that person subscribes to), a tier ('primary'/'secondary')
    and an optional weekly window (days + start/end 'HH:MM'). Deliberately
    strict: a rota with a typo'd day would silently mean "nobody is on call",
    which is exactly the failure a compliance audit must not be able to hide."""
    if not isinstance(value, list):
        raise ValueError(
            f"oncall.rota must be a list of rota entries, got {type(value).__name__}")
    _valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun", "all"}

    def _hhmm(label, v) -> None:
        if not isinstance(v, str):
            raise ValueError(f"{label} must be 'HH:MM', got {v!r}")
        parts = v.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"{label} must be 'HH:MM', got {v!r}")
        try:
            hh, mm = int(parts[0]), int(parts[1])
        except ValueError:
            raise ValueError(f"{label} must be numeric 'HH:MM', got {v!r}")
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"{label} out of range: {v!r}")

    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(
                f"oncall.rota[{i}] must be an object, got {type(entry).__name__}")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"oncall.rota[{i}] needs a non-blank name")
        topic = entry.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError(
                f"oncall.rota[{i}] ({name}) needs a non-blank ntfy topic")
        tier = entry.get("tier", "primary")
        if tier not in ("primary", "secondary"):
            raise ValueError(
                f"oncall.rota[{i}] ({name}) tier must be 'primary' or "
                f"'secondary', got {tier!r}")
        days = entry.get("days", "all")
        if isinstance(days, str):
            days = [days]
        if not isinstance(days, list) or not days:
            raise ValueError(
                f"oncall.rota[{i}] ({name}) days must be 'all' or a "
                f"non-empty list, got {days!r}")
        for d in days:
            if not isinstance(d, str) or d.strip().lower() not in _valid_days:
                raise ValueError(
                    f"oncall.rota[{i}] ({name}) day {d!r} is not one of "
                    "mon..sun/all")
        start, end = entry.get("start"), entry.get("end")
        if (start is None) != (end is None):
            raise ValueError(
                f"oncall.rota[{i}] ({name}) must give start AND end "
                "together (or neither = always on call)")
        if start is not None:
            _hhmm(f"oncall.rota[{i}].start", start)
            _hhmm(f"oncall.rota[{i}].end", end)


register(
    "oncall.rota",
    list,
    [],
    validator=_validate_oncall_rota,
    restart_required=False,
    group="gsec",
    description="GSEC FX.119 p3b (Q106=A) — the on-call ROTA as a JSON list. "
                "Each entry: {name, topic, tier 'primary'|'secondary', "
                "days 'all'|['mon'..'sun'], start 'HH:MM', end 'HH:MM'} "
                "(start/end omitted = on call around the clock; end before "
                "start wraps past midnight, e.g. a night shift 20:00->06:00). "
                "A page goes to the entry whose window covers NOW; ties break "
                "by list order. Empty list (default) = NO rota, so pages "
                "fall back to oncall.owner_topic and the gap is audited "
                "ONCALL_NO_ROTA once per window.",
)
register(
    "oncall.owner_topic",
    str,
    "",
    group="gsec",
    description="GSEC FX.119 p3b/p3a (Q106=A) — the ESCALATION target: the "
                "ntfy topic the owner/site manager subscribes to, always "
                "reachable. Used twice: (p3b) the fallback when no rota "
                "entry covers NOW, and (p3a) the 15-minute escalation re-page "
                "when nobody acked. Empty = no escalation target configured "
                "(the page stays on its original topic and says so).",
)
def _validate_oncall_timezone(value) -> None:
    """A named zone that actually resolves. A rota whose zone cannot be
    resolved would make every window check fall back to UTC silently — the
    exact class of bug where a night shift pages the wrong person for an
    hour. zoneinfo is stdlib (Python 3.11) and the container has it."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("oncall.timezone must be a non-blank IANA zone name")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(value.strip())
    except Exception as exc:  # noqa: BLE001 — the validator IS the check
        raise ValueError(
            f"oncall.timezone {value!r} is not a resolvable IANA zone: {exc}")


register(
    "oncall.timezone",
    str,
    "UTC",
    validator=_validate_oncall_timezone,
    restart_required=False,
    group="gsec",
    description="GSEC FX.119 p3b (Q106=A) — IANA zone the rota's start/end "
                "hours and days are written in (e.g. 'America/New_York' for "
                "a US site, 'UTC' for the colo). A page's 'who is on call' "
                "answer is meaningless without it, so it is a setting, not a "
                "guess. Default UTC.",
)

# ---------------------------------------------------------------------------
# GSEC FX.119 p3a (owner decision Q106=A) — the ACK WINDOW + ESCALATION.
# A page is not "delivered" until someone ACKs it; the owner decision's
# guarantee is 15 minutes: a page nobody acked inside oncall.ack_window_
# seconds is re-paged to oncall.owner_topic EXACTLY ONCE (the escalation is
# one hop, never a chain — MOD must never turn a monitor alert into an
# infinite ping-pong between two phones). The sweep cadence is a setting so
# an operator can dial the clock's resolution without a deploy.
# ---------------------------------------------------------------------------


def _validate_oncall_ack_window_seconds(value) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            "oncall.ack_window_seconds must be an int (seconds), "
            f"got {value!r}")
    if value < 60 or value > 86400:
        raise ValueError(
            "oncall.ack_window_seconds must be between 60 and 86400 "
            f"seconds, got {value}")


def _validate_oncall_escalation_check_seconds(value) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            "oncall.escalation_check_seconds must be an int (seconds), "
            f"got {value!r}")
    if value < 0 or value > 86400:
        raise ValueError(
            "oncall.escalation_check_seconds must be 0 (sweep OFF) or "
            f"between 1 and 86400 seconds, got {value}")


register(
    "oncall.ack_window_seconds",
    int,
    900,
    validator=_validate_oncall_ack_window_seconds,
    restart_required=False,
    group="gsec",
    description="GSEC FX.119 p3a (Q106=A) — how long a page's ack window "
                "stays OPEN before the escalation sweep fires (seconds). "
                "Default 15 min = 900. Floor 60s: a window "
                "shorter than a minute means the on-call person cannot "
                "physically tap their phone before the owner is paged.",
)
def _validate_oncall_ack_base_url(value) -> None:
    """The origin the ACK LINK is built on. Deliberately its own setting
    rather than reusing oncall.push_base_url: the push stack's origin is
    ntfy's, not the API's, and an ack link that points at ntfy is a dead
    end on the phone. Empty = no ack link is attached (the page still
    records, it just cannot be acked from the phone)."""
    if not isinstance(value, str):
        raise ValueError("oncall.ack_base_url must be a string origin")
    v = value.strip()
    if not v:
        return
    if not v.startswith(("http://", "https://")):
        raise ValueError(
            f"oncall.ack_base_url must be an http(s) origin, got {value!r}")


register(
    "oncall.ack_base_url",
    str,
    "",
    validator=_validate_oncall_ack_base_url,
    restart_required=False,
    group="gsec",
    description="GSEC FX.119 p3a (Q106=A) — origin the ACK LINK is built on "
                "(the API's own mesh/LAN origin, e.g. "
                "http://203.0.113.42:8000 — NOT the ntfy stack's origin). "
                "The page's Click URL + ntfy Actions header point at "
                "POST {this}/v1/oncall/pages/{id}/ack?token=... . Empty = no "
                "ack link attached (the page still records; it just cannot "
                "be acked from the phone).",
)
register(
    "oncall.escalation_check_seconds",
    int,
    60,
    validator=_validate_oncall_escalation_check_seconds,
    restart_required=False,
    group="gsec",
    description="GSEC FX.119 p3a (Q106=A) — cadence (seconds) of the "
                "escalation sweep job (fx119_oncall_escalation): it re-pages "
                "oncall.owner_topic for a 'sent' page past its ack window, "
                "marks it escalated and audits ONCALL_PAGE_ESCALATED. 0 = "
                "the sweep is OFF (job stays registered, no-ops). Default "
                "60; 0..86400.",
)

# ---------------------------------------------------------------------------
# GSEC FX.119 p2b — MONITOR -> PAGE wiring switches. The paging CLIENT
# (services/paging_service.send_page) and the channel itself are gated by
# oncall.paging_enabled (default OFF until the ntfy stack is deployed);
# these three say WHICH detectors may raise a page at all, so a monitor can
# be silenced without touching its audit/email path. All default True: once
# the channel exists, every Sev-1-class incident the fleet detects pages it.
# ---------------------------------------------------------------------------
register(
    "oncall.security_alert_paging_enabled",
    bool,
    True,
    group="gsec",
    description="GSEC FX.119 p2b — when true, an FX.156 security alert "
                "raised at severity high|critical by the rule sweep also "
                "pages the on-call stack (priority sev2/sev1, dedupe key "
                "rule:tenant:window). The alert row, its email and the "
                "daily digest are unaffected either way; a paging failure "
                "never breaks the alert. Default True.",
)
register(
    "oncall.sev1_ticket_paging_enabled",
    bool,
    True,
    group="gsec",
    description="GSEC FX.119 p2b — when true, a Sev-1 support ticket on an "
                "owner-on-call tenant (the Q53=A 24/7 owner-rotation tier) "
                "pages the on-call stack the moment it is filed. Non-owner-on-call "
                "tiers never page from here (their SLA is business-hours, "
                "staffed, not owner-on-call). The ticket + its audit row + "
                "auto-ack email are unaffected; a paging failure never "
                "breaks ticket creation. Default True.",
)
register(
    "oncall.monitor_paging_enabled",
    bool,
    True,
    group="gsec",
    description="GSEC FX.119 p2b — when true, the platform health watchdogs "
                "that already raise an admin alert (GPU pool queue breach, "
                "Vault seal-state transition, cert-expiry window entry) also "
                "page the on-call stack. Each keeps its own audit row + email "
                "regardless; the page is the third channel to the phone. "
                "Default True.",
)


# ---------------------------------------------------------------------------
# FX.117 p3a (owner Q104=A) — exact-value masking of tenant secret VALUES.
#
# The customer secret store injects a tenant's secret VALUES into the
# consuming node. A node that echoes the credential into its stdout/output
# used to persist it verbatim in NodeRun.inputs / .outputs / .error_message
# (and the API then serves it back to the operator). Pattern redaction
# (utils.secret_redaction.redact) only catches values that LOOK like a secret,
# so a bare opaque credential slipped through — part 1 closed that for the
# worker TASK report; this setting gates the same exact-value mask at the
# NODE-RUN ingest point (crud/node_run_cud.update_node_run_status), which is
# where the API persists node logs/outputs/errors.
#
# On (the default) the run's referenced secret values — and their base64 /
# URL-encoded spellings — are replaced with "***" before storage. Off is the
# explicit opt-out for an install that needs raw node output for debugging;
# it is a platform-scope knob only, so a tenant cannot weaken the install.
# ---------------------------------------------------------------------------
register(
    "secrets.mask_values_in_logs",
    bool,
    True,
    group="secrets",
    description="FX.117 p3a — when true, any exact occurrence of a run's "
                "referenced tenant-secret VALUE (plus its base64 / URL-encoded "
                "forms) is replaced with '***' in the node logs, error "
                "messages and string outputs the API persists and returns "
                "(NodeRun.inputs / NodeRun.outputs / NodeRun.error_message). "
                "Values shorter than 4 chars are skipped to avoid masking "
                "ordinary prose. Default True; false is an explicit opt-out "
                "for installs that need raw node output.",
)


# ---------------------------------------------------------------------------
# GSEC FX.112 p5a — gov/DoD FLOOR for the FX.112 tenant-admin alert rules
# (services/tenant_alert_rules.py + services/security_alert_engine.py).
# While a gov/DoD compliance profile is active (cmmc / fedramp_high_il5 —
# the same list as services/credential_lifetime.GOV_PROFILES and
# services/security_incidents.GOV_PROFILES), the tenant-effective alert
# defaults are TIGHTER:
#   * every tenant-applicable rule is forced ON (a tenant cannot disable
#     one while gov is active);
#   * a count threshold fires at >= threshold, so a LOWER value is
#     tighter: the gov floor is the tightest value the tenant must at
#     least match, and a tenant override LOOSER than it (a higher
#     threshold) is refused with 422 — same refuse-not-clamp semantics as
#     the p3a platform bounds and credential_lifetime's gov cap;
#   * the failed-sign-in / export-anomaly windows may not run LONGER than
#     the gov window (the window is platform-level, so the engine clamps
#     it instead of refusing a write).
# Strengthening (a tighter threshold, a shorter window) is always allowed.
# No env vars: these are DB platform settings, defaults mirrored in
# services/tenant_alert_rules.py.
# ---------------------------------------------------------------------------
register(
    "security_alerts.gov_signin_threshold_floor",
    int,
    3,
    group="security",
    description="GSEC FX.112 p5a — gov/DoD floor for the per-tenant "
                "signin_failures threshold: while a gov/DoD profile is "
                "active the tenant's effective failed-sign-in threshold "
                "is at most this (a lower tenant override is honoured, a "
                "loosening one is refused with 422). Default 3 (the "
                "tightest p3a bound), vs the 10 platform default.",
)
register(
    "security_alerts.gov_download_threshold_floor",
    int,
    10,
    group="security",
    description="GSEC FX.112 p5a — gov/DoD floor for the per-tenant "
                "unusual_downloads threshold (bulk downloads/exports per "
                "user over the trailing hour). While a gov/DoD profile "
                "is active the tenant's effective threshold is at most "
                "this; a looser override is refused with 422. Default "
                "10, vs the 50 platform default.",
)
register(
    "security_alerts.gov_export_min_count_floor",
    int,
    10,
    group="security",
    description="GSEC FX.112 p5a — gov/DoD floor for the per-tenant "
                "export_anomaly absolute min count (dlp.export_anomaly_"
                "min_count). While a gov/DoD profile is active the "
                "tenant's effective floor is at most this; a looser "
                "override is refused with 422. Default 10, vs the 20 "
                "platform default.",
)
register(
    "security_alerts.gov_signin_window_max_minutes",
    int,
    10,
    group="security",
    description="GSEC FX.112 p5a — longest failed-sign-in burst window "
                "allowed while a gov/DoD profile is active: the engine "
                "clamps security_alerts.signin_failure_window_minutes to "
                "this (a shorter platform window is honoured). Default "
                "10 minutes, vs the 15-minute platform default.",
)
register(
    "security_alerts.gov_export_window_max_minutes",
    int,
    30,
    group="security",
    description="GSEC FX.112 p5a — longest export-anomaly window allowed "
                "while a gov/DoD profile is active: the engine clamps "
                "dlp.export_anomaly_window_minutes to this. Default 30 "
                "minutes, vs the 60-minute platform default.",
)


# ---------------------------------------------------------------------------
# FX.108 p1 (owner decision Q95=A) — hosted sign-up AGE ATTESTATION.
#
# Q95=A: "hosted-cloud sign-up requires age 16+ (tick-box confirmation),
# under-13s never on the hosted cloud". MOD is a COPPA-style regime: an
# account on the HOSTED cloud must prove it belongs to someone 16 or older
# BEFORE the account exists, so the self-serve sign-up (POST
# /onboarding/create-new-tenant and /onboarding/finalize-invited-user)
# carries an age tick and services/signup_age_attestation.py enforces it.
#
# HOSTED ONLY. Both settings are read inside a `utils.entitlements.is_saas`
# guard: an on-prem install has no hosted signup and the owner decision
# scopes the age rule to the hosted cloud (K-12 / under-13 use is on-prem
# only, under the school's own authority — FX.108 p3's education profile),
# so an on-prem sign-up that sends no attestation at all is UNCHANGED.
#
# `signup.minimum_age` is the bar, not the wire format: the form and the ToS
# age clause (FX.108 p2) both promise the single 16+ tick, so the hosted path
# accepts only that tick. Raising the bar above 16 therefore cannot be
# satisfied by the plain tick — it is a compliance-profile / education knob,
# never a way to loosen the hosted form. Lowering it below 16 is refused
# outright: under-13s are never on the hosted cloud, per the owner decision.
# ---------------------------------------------------------------------------
def _validate_signup_minimum_age(value: int) -> None:
    """13..19 only. Below 13 would put the HOSTED cloud itself under a child-
    data regime (the owner decision says under-13s are never hosted); above
    19 is an unusable bar nobody can tick the 16+ box against."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"signup.minimum_age must be an integer, got {value!r}")
    if value < 13 or value > 19:
        raise ValueError(
            f"signup.minimum_age must be between 13 and 19 (under-13s are "
            f"never on the hosted cloud), got {value!r}")


register(
    "signup.age_attestation_required",
    bool,
    True,
    group="gsec",
    description="FX.108 p1 (Q95=A) — when true (the default), a HOSTED self-serve "
                "sign-up must carry the 16+ age tick before any account is "
                "created: a missing tick is a 422 form error and an underage "
                "attempt is a neutral 403 whose audit row carries only the "
                "band (no name, email, birth year or IP — a refused attempt "
                "stores no personal data at all). Only applies when the "
                "deployment is hosted (DEPLOYMENT_MODE=saas); on-prem sign-ups "
                "are untouched. false is the explicit kill switch for an "
                "install that must accept un-attested hosted sign-ups — a "
                "read failure falls back to true (the gate fails CLOSED).",
)
register(
    "signup.minimum_age",
    int,
    16,
    group="gsec",
    validator=_validate_signup_minimum_age,
    description="FX.108 p1 (Q95=A) — the age bar the hosted sign-up attestation "
                "must clear (default 16, 'hosted-cloud "
                "sign-up requires age 16+'). Accepted range 13..19: 13 is the "
                "floor the COPPA-style regime allows on the hosted cloud "
                "(under-13s are never hosted), and the "
                "band between the bar and 16 is refused with SIGNUP_UNDERAGE_BLOCKED. "
                "The WIRE value stays the single 16+ tick the form and the ToS "
                "age clause promise, so raising the bar above 16 is a "
                "compliance-profile / education knob, not a looser form. Hosted "
                "only; on-prem ignores it.",
)
# The gov profile restricts the AI assistant to
# local models (MOD stacks / on-box endpoints), never external/cloud LLM
# providers. Read by services.assistant_gov_models, which gates
# services.ai_assistant_service.dispatch_ai_task: under an active
# gov/DoD compliance profile (cmmc / fedramp_high_il5) an assistant LLM
# binding that resolves to an external endpoint — or to no endpoint at
# all — is refused before enqueue with a clear message + an audited
# refusal. No silent fallback to cloud, ever.
register(
    "assistant.gov_local_models_only",
    bool,
    True,
    group="ai",
    description="When a gov/DoD compliance profile is "
                "active (cmmc / fedramp_high_il5), the in-app AI assistant may "
                "only use LOCAL models — MOD stacks / on-box endpoints (mesh "
                "MagicDNS, tailnet, docker service aliases, localhost, "
                "RFC1918/CGNAT/link-local, managed local-LLM aliases). "
                "External / cloud LLM bindings are refused at dispatch with a "
                "clear message and an audit row, and no local model available "
                "is refused too — never a silent fallback to cloud. This "
                "setting is the install-level switch: True (default) enforces "
                "the posture; False restores the pre-gate behaviour. Non-gov "
                "installs are unaffected either way. Read by "
                "services.assistant_gov_models.posture_active.",
)

# ---------------------------------------------------------------------------
# GSEC FX.120 p5b (owner decision Q107=A) — the WEEKLY ELEVATION SUMMARY
# SEND. p2a/p2b/p2c made elevation a JIT, auto-revoking, second-person
# ledger; p5b closes the loop by REPORTING it: services/
# privileged_elevation_summary.build_weekly_summary reads the ledger + the
# PRIVILEGED_ELEVATION_* audit trail for the previous 7 days and
# send_weekly_summary emails the ACTIVE platform admins (the same
# recipient set FX.70 part 4 / break-glass use — this ledger is PLATFORM-
# scoped, so there is no tenants.security_contact_email to route to). The
# send writes one ADMIN_SYSTEM_MAINTENANCE audit row (an existing value — no enum
# change, no migration) whose metadata carries the numeric payload AND
# doubles as the dedupe record: a second tick in the same ISO week sends
# nothing again. No env vars: every knob is a platform setting, re-read
# EVERY pass, so moving the send or turning it off needs no restart.
# ---------------------------------------------------------------------------


def _validate_fx120_summary_weekday(value) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"elevation.weekly_summary_weekday must be an int (0=Mon..6=Sun), "
            f"got {value!r}")
    if not 0 <= value <= 6:
        raise ValueError(
            f"elevation.weekly_summary_weekday must be between 0 and 6, "
            f"got {value}")


def _validate_fx120_summary_hour(value) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"elevation.weekly_summary_hour_utc must be an int (UTC hour), "
            f"got {value!r}")
    if not 0 <= value <= 23:
        raise ValueError(
            f"elevation.weekly_summary_hour_utc must be between 0 and 23, "
            f"got {value}")


register(
    "elevation.weekly_summary_enabled",
    bool,
    True,
    restart_required=False,
    group="security",
    description="GSEC FX.120 p5b (Q107=A) — when true (the default), the "
                "weekly privileged-elevation summary is sent to the active "
                "platform admins on the configured day/hour (UTC). false "
                "turns the SEND off (the ledger, the audit trail and the "
                "auto-revoke sweep are untouched — reporting is a read-out, "
                "never a grant). Re-read every scheduler pass, so switching "
                "needs no restart. Default true.",
)
register(
    "elevation.weekly_summary_weekday",
    int,
    0,
    validator=_validate_fx120_summary_weekday,
    restart_required=False,
    group="security",
    description="GSEC FX.120 p5b — day of week (0=Mon..6=Sun) the weekly "
                "privileged-elevation summary fires. Default 0 (Monday), "
                "matching the FX.70 weekly access-review cadence so the two "
                "reports are read together.",
)
register(
    "elevation.weekly_summary_hour_utc",
    int,
    8,
    validator=_validate_fx120_summary_hour,
    restart_required=False,
    group="security",
    description="GSEC FX.120 p5b — hour (UTC) the weekly privileged-elevation "
                "summary fires. Default 8 (08:00 UTC Mondays, the same slot "
                "the FX.70 support report uses); 1..23 keeps it clear of the "
                "Monday 03:00 weekly-overage run.",
)

# ---------------------------------------------------------------------------
# GSEC FX.117 p4a/p4b (owner Q104=A) — per-secret EXPIRY as a RECORD
# (models.SecretExpiry / table `secret_expiries`, migration
# 20261007zzzf_fx117_p4_secret_expiries). Same shape as p3b's scope table:
# models.py is bind-mounted live, so NO column was added to the existing
# `secrets` table — the expiry date an admin sets lives in the new table and
# the p2 `Secret.expires_at` bag stays as the admin-facing view it mirrors.
#
# Enforcement lives in services/secret_expiry_service.py:
#   * block_expired            — the run-token gate (routers/worker_tasks.py
#     resolve_task_secrets, beside p3b's assert_run_scope) refuses an EXPIRED
#     secret: the value is never injected, SECRET_ACCESS_DENIED reason=expired
#     is audited and the node fails with "secret <name> has expired". Default
#     TRUE — silently injecting an expired credential is the unsafe direction,
#     so the switch ships on and a failed settings read keeps enforcement ON.
#   * expiry_reminder_interval_hours — the p4b reminder job
#     (services/scheduler.py job fx117_secret_expiry_reminders) notifies the
#     tenant admins AT MOST ONCE per this window, via the live row's
#     `last_reminded_at` stamp. Default 24 h.
#
# The LEAD band reuses the p2 `secrets.expiry_reminder_days` key registered
# above (a list of day-marks, default [3, 14]): p4b takes the WIDEST mark as
# its band, so the default band is 14 days exactly as the owner decision asks
# and the p2 per-day sweep keeps working off the same setting. Re-registering
# an existing key is a no-op in this registry, so p4 does NOT re-declare it;
# services/secret_expiry_service.py::_reminder_days accepts either an int or
# that list shape so a tenant that sets a plain int here is honoured too.
#
# No env vars: tenant-scoped DB platform settings, re-read every pass.
# ---------------------------------------------------------------------------
def _validate_fx117p4_interval_hours(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            "secrets.expiry_reminder_interval_hours must be an int (hours), "
            f"got {value!r}"
        )
    if not 1 <= int(value) <= 8760:
        raise ValueError(
            "secrets.expiry_reminder_interval_hours must be in 1..8760 "
            f"(1 hour .. 1 year), got {value!r}"
        )


register(
    "secrets.block_expired",
    bool,
    True,
    restart_required=False,
    group="secrets",
    description="GSEC FX.117 p4a (Q104=A) — when true (the default), a secret "
                "whose live `secret_expiries` row is at or before now is "
                "REFUSED at run-time secret injection: the value is never "
                "returned to the worker, SECRET_ACCESS_DENIED reason=expired "
                "is audited and the node fails with 'secret <name> has "
                "expired'. false turns the enforcement off (the expiry stays "
                "a record, the reminders still fire and the admin UI still "
                "shows it) — an operator choice, not the default. Re-read "
                "every resolution, so switching needs no restart; a failed "
                "settings read keeps enforcement ON.",
)
register(
    "secrets.expiry_reminder_interval_hours",
    int,
    24,
    validator=_validate_fx117p4_interval_hours,
    restart_required=False,
    group="secrets",
    description="GSEC FX.117 p4b — the minimum gap between two expiry "
                "reminders for the SAME secret (the live `secret_expiries` "
                "row's last_reminded_at is the dedupe record). The job "
                "fx117_secret_expiry_reminders notifies the tenant admins "
                "(in-app notification + email) for every secret expiring "
                "within secrets.expiry_reminder_days or already expired, at "
                "most once per this window. Default 24 h; 1 h is the "
                "stormiest a tenant may pick.",
)

# ---------------------------------------------------------------------------
# GSEC FX.117 p4c (gov wrap) — the gov/DoD secret-lifetime ceiling.
#
# While a gov/DoD compliance profile is active (cmmc / fedramp_high_il5 —
# the same list as services/credential_lifetime.GOV_PROFILES,
# services/tenant_alert_rules.GOV_PROFILES and
# services/security_incidents.GOV_PROFILES), every tenant secret must carry
# an expiry no further out than this many days:
#   * POST /secrets/ (create) with no expires_at, or one beyond this ceiling,
#     is refused 422 (services/secrets_service.create_secret via
#     services/secret_expiry_service.assert_gov_expiry);
#   * PATCH /secrets/{id} that would leave the secret without an expiry, or
#     that sets one beyond it, is refused 422;
#   * PUT /secrets/{id}/expiry setting a date beyond it is refused 422.
#
# Existing secrets that never got an expiry are NOT auto-expired: the p4b
# reminder job (services/secret_expiry_service.expiry_reminder_tick) reports
# them to the tenant admins as "missing expiry" (secret NAME only) once per
# secrets.expiry_reminder_interval_hours.
#
# No env vars: DB-backed platform setting, re-read every pass.
# ---------------------------------------------------------------------------
def _validate_fx117p4c_gov_max_days(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            "secrets.gov_max_lifetime_days must be an int (days), "
            f"got {value!r}"
        )
    if not 1 <= int(value) <= 3650:
        raise ValueError(
            "secrets.gov_max_lifetime_days must be in 1..3650 "
            f"(1 day .. 10 years), got {value!r}"
        )


register(
    "secrets.gov_max_lifetime_days",
    int,
    365,
    validator=_validate_fx117p4c_gov_max_days,
    restart_required=False,
    group="secrets",
    description="GSEC FX.117 p4c — the gov/DoD secret-lifetime ceiling (days). "
                "While a gov/DoD compliance profile (cmmc / fedramp_high_il5) "
                "is active, every tenant secret MUST carry an expiry at most "
                "this many days out: creating or updating a secret without an "
                "expiry, or setting one further out than this, is refused "
                "with 422. Default 365. Secrets that already exist without "
                "an expiry are never auto-expired — the expiry-reminder job "
                "reports them to the tenant admins as 'missing expiry' (name "
                "only). Re-read every pass, so switching needs no restart.",
)

# ---------------------------------------------------------------------------
# UNTRUSTED-CONTENT TAGGING + FORCED
# CONFIRMATION for risky assistant tool calls in "tainted" turns
# (prompt-injection defence). Tools whose results carry external / user-
# authored data (log tails, docs/web/repo fetches, user-uploaded knowledge-
# base file contents) are classified in assistant_confirm.TOOL_UNTRUSTED_
# SOURCE: the model-bound copy is wrapped in <untrusted source="...">…
# </untrusted> delimiters (delimiter text escaped inside) with a system note
# that instructions inside untrusted blocks must not be followed, and the
# turn is marked TAINTED. In a tainted turn, RISKY tool calls — anything
# that writes / deletes / executes / sends / changes permissions or
# settings, unknown tools defaulting to risky — need explicit user
# confirmation through the existing one-time confirm-token mechanism even if
# they normally would not; read-only calls proceed. Each forced confirmation
# is audited (SUSPICIOUS_ACTIVITY, via=assistant). No env vars: this is a
# DB-backed platform setting, re-read every turn — False restores the
# pre-p3a behaviour (no wrapping, no forcing); a failed read keeps it ON.
# ---------------------------------------------------------------------------
register(
    "assistant.untrusted_confirm_enabled",
    bool,
    True,
    restart_required=False,
    group="ai",
    description="The assistant prompt-injection "
                "defence: untrusted tool output (logs, docs/web fetches, "
                "user-uploaded KB files) is wrapped in <untrusted> "
                "delimiters before it reaches the model, and in a tainted "
                "turn (untrusted content in context) risky tool calls — "
                "write/delete/execute/send/permission-setting changes, "
                "unknown tools included — force explicit user confirmation "
                "through the existing confirm-token gate while read-only "
                "calls proceed; each forcing is audited via=assistant. "
                "True (the default) keeps the defence on; False restores "
                "the pre-p3a behaviour (no wrapping, no forcing). Re-read "
                "every turn, so switching needs no restart; a failed "
                "settings read keeps the defence ON. Read by "
                "services.assistant_confirm (untrusted_confirm_enabled / "
                "check_tool_call) and wired at the assistant tool-dispatch "
                "point in services.assistant_orchestrator.py.",
)


# ---------------------------------------------------------------------------
# GSEC FX.74 p3a — fail-safe posture when the offline OFAC SDN list is
# missing/unreadable at screening time (screening cannot prove anyone clean
# without a list). Enforcement lives in services/sanctions_screening.py
# (_missing_list). "" = auto: hold for review when a gov/DoD compliance
# profile (cmmc / fedramp_high_il5) is active, otherwise log one audit row
# (kind sanctions_sdn_screening_list_missing, no email) and allow.
# ---------------------------------------------------------------------------

def _validate_missing_list_policy(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("sanctions.missing_list_policy must be a string")
    if value.strip().lower() not in ("", "hold", "allow"):
        raise ValueError("sanctions.missing_list_policy must be '', "
                         "'hold' or 'allow'")


register(
    "sanctions.missing_list_policy",
    str,
    "",
    group="gsec",
    validator=_validate_missing_list_policy,
    description="GSEC FX.74 p3a: what screening does when the local OFAC SDN "
                "CSV is missing or unreadable (it cannot prove anyone clean). "
                "'hold' = hold the account for review (audit row, user "
                "status PENDING, empty matches = the LIST was unavailable); "
                "'allow' = allow and log one audit row "
                "(kind sanctions_sdn_screening_list_missing, no email); "
                "'' (the default) = auto: hold when a gov/DoD compliance "
                "profile (cmmc / fedramp_high_il5) is active, otherwise "
                "allow-with-log. Read by services/sanctions_screening.py.",
)

# ---------------------------------------------------------------------------
# FX.91 p5 [B] (owner decision 10-07, Tim Q5=A) — UNDECLARED NODES GET NO
# EXTERNAL NETWORK UNDER A GOV PROFILE (fail closed)
# p3a capped what a node may DECLARE (declared domains outside
# nodes.egress_allowlist_domains are a hard block) but it is INERT for a node
# that declares NOTHING: with an empty egress_domains there is nothing to
# block, so today such a node still gets external reach whenever the worker
# promotes it to the main network via the API_KEY / MOD_TASK_TOKEN_URL /
# NIM_API_KEY markers (CORE/Worker/internal/executor/netpol.go:104/116/123 —
# all three branch BEFORE the AllowedEgress check at :138). Q5=A closes that
# hole: under an active gov/DoD compliance profile, a node declaring NO
# egress_domains gets NO external network at all — internal callback to the
# MOD API / task-token endpoints must still work (nodes must still report
# results), so this is "no EXTERNAL network", not "no network". The worker
# expresses that with the existing internal-only posture net
# mod_stack-api-net (Internal: true, the API attached, no internet —
# services/k8s_stack_netpol.py:136 mode 'api' is the same posture, and the
# k8s backend already enforces it that way). Default True; cmmc +
# fedramp_high_il5 FORCE it on (PROFILE_REQUIRED_SETTINGS, same shape as
# p3a's nodes.egress_gov_allowlist_enforced); fedramp_moderate does not.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GSEC FX.135 p2 (owner decision Q123=A) — ONE FREE TRIAL PER CARD
# Q123=A "hosted-cloud fraud and compute abuse": "Throwaway email domains
# blocked; one free trial per card or device." The card half: Stripe hands
# us a stable per-card token (`payment_method.card.fingerprint` — same value
# for the same physical card across every customer), so the trial-create
# path refuses a card that another tenant already used for a free trial and
# records the fingerprint otherwise. The record itself is the NEW table
# `trial_card_fingerprints` (models.TrialCardFingerprint, migration
# 20261007zzzg_fx135_p2_trial_card_fingerprints.py). This key is the kill
# switch: off = the rule is not enforced (every signup gets its trial).
# Default True = the owner's rule ships ON. Hosted-only in spirit (on-prem
# signups have no Stripe card at all, so there is no fingerprint to check
# and the path is inert by construction). No env var: the tunable is the
# DB-backed setting only.
# ---------------------------------------------------------------------------
register(
    "fraud.one_trial_per_card",
    bool,
    True,
    group="gsec",
    description="FX.135 p2 (Q123=A): while True, a free trial cannot be started "
                "with a payment-card fingerprint another tenant already used for "
                "one (record: trial_card_fingerprints, one LIVE row per card). "
                "No fingerprint (no card on file yet — on-prem, or a signup "
                "before checkout) = allowed, nothing recorded. False = rule "
                "off, every signup gets its trial. Default True.",
)

# ---------------------------------------------------------------------------
# GSEC FX.135 s3 — COMPUTE-ABUSE (CRYPTO-MINING) SIGNATURE KNOBS
# Q123=A ("hosted-cloud fraud and compute abuse") again; the compute-abuse
# half. A crypto-miner behind a MOD subscription looks like a legitimate
# heavy-GPU job for ONE sample, so the signature is a conjunction: GPU
# utilisation >= 95% held SUSTAINED for `fraud.mining_gpu_sustained_minutes`
# AND the tenant's outbound egress over `fraud.mining_egress_gb_threshold`
# GB (services/mining_signature_detection.py reads the worker's
# TaskResourceSample.gpu_util_pct stream + TenantEgressUsageDaily.egress_bytes).
# The first key is the kill switch: off = the detector does nothing at all
# (no telemetry read, no hold, no ticket). The other two are pure tunables.
# No env vars for behaviour — DB-backed only. The scheduler job that calls
# run_sweep() is wired by the lead, NOT here.
# ---------------------------------------------------------------------------
register(
    "fraud.mining_gpu_sustained_minutes",
    int,
    30,
    group="gsec",
    description="FX.135 s3 — how long GPU utilisation must stay >= 95% "
                "(contiguously, on the worker's 30s TaskResourceSample "
                "stream) before the compute-abuse signature can fire. A "
                "shorter spike never qualifies. Default 30 minutes.",
)
register(
    "fraud.mining_egress_gb_threshold",
    float,
    50.0,
    group="gsec",
    description="FX.135 s3 — the sustained-GPU half only flags when the "
                "tenant's metered egress (TenantEgressUsageDaily.egress_bytes) "
                "OVER this many gigabytes is exceeded in the same window. "
                "Default 50 GB.",
)
register(
    "fraud.mining_detection_enabled",
    bool,
    False,
    group="gsec",
    description="FX.135 s3 — kill switch for the crypto-mining signature "
                "detector (services/mining_signature_detection.run_sweep). "
                "True = a sustained >=95% GPU grind combined with over-"
                "threshold egress holds the tenant's GPU work (existing "
                "WorkflowRun.paused + ApiTaskQueue cancellation) and opens "
                "ONE abuse ticket per tenant per 24h. False = the sweep is "
                "inert: no telemetry read, no hold, no ticket. OFF BY "
                "DEFAULT — the platform tenant is excluded either way; the "
                "owner turns it on.",
)
register(
    "fraud.mining_sweep_interval_minutes",
    int,
    10,
    group="gsec",
    validator=_validate_positive_int,
    description="FX.135 s4 — minutes between scheduled passes of the "
                "crypto-mining signature sweep (services/scheduler.py job "
                "mining_signature_sweep, cadence read at scheduler start). "
                "Must be a real cadence (>= 1); the kill switch is "
                "fraud.mining_detection_enabled, not this knob. Default 10.",
)

# ---------------------------------------------------------------------------
# GSEC FX.135 s5 (owner decision Q123=A) — NEW-TENANT GPU SPEND / CONCURRENCY
# Q123=A ("hosted-cloud fraud and compute abuse") again: the "throwaway tenant"
# half. A brand-new tenant (no verified payment history yet — the SAME flag
# FX.135 p2 uses on the trial-create path: the tenant's subscription is still
# a free trial / has never attached a real Stripe card) gets a GPU budget
# until payment history is verified: at most `fraud.new_tenant_gpu_concurrency_limit`
# GPU tasks outstanding at once, and at most `fraud.new_tenant_gpu_spend_limit_usd`
# of metered GPU spend (CreditUsageLog.cost_usd_cents) in the current billing
# period. Verified payment history = no cap at all (the plan's own limits apply,
# unchanged). Both keys are pure tunables; the kill switch is the verified-history
# check itself, so a verified tenant never reads them. No env vars for behaviour
# — DB-backed only. Enforced by services/billing_service.new_tenant_gpu_guard,
# called from the dispatch choke point routers/worker_tasks.enqueue_task_internal.
# ---------------------------------------------------------------------------
def _validate_non_negative_number(value: float) -> None:
    if value < 0:
        raise ValueError(f"{value!r} must be a non-negative number")


register(
    "fraud.new_tenant_gpu_concurrency_limit",
    int,
    2,
    group="gsec",
    validator=_validate_non_negative_int,
    description="FX.135 s5 (Q123=A) — GPU tasks a tenant WITHOUT verified "
                "payment history may have outstanding (pending/assigned/claimed/"
                "running) at the same time. Over the limit the enqueue is refused "
                "with a clear error (no silent drop). 0 = a new tenant gets no "
                "GPU work at all. A tenant with verified payment history never "
                "consults this key — the plan's own concurrency limits apply. "
                "Default 2.",
)
register(
    "fraud.new_tenant_gpu_spend_limit_usd",
    float,
    25.0,
    group="gsec",
    validator=_validate_non_negative_number,
    description="FX.135 s5 (Q123=A) — USD of metered GPU spend (CreditUsageLog."
                "cost_usd_cents for the current billing period) a tenant WITHOUT "
                "verified payment history may cross before further GPU work is "
                "refused. Over the limit the enqueue is refused with a clear "
                "error naming the limit; 0 = no GPU spend at all before payment "
                "history is verified. Verified tenants are uncapped. Default "
                "25.00 USD.",
)


# ---------------------------------------------------------------------------
# GSEC FX.135 s1 — DISPOSABLE-EMAIL BLOCK AT SELF-SERVICE SIGN-UP.
#
# A sign-up on a throwaway domain (mailinator / yopmail / 10minutemail / ...)
# has no permanent identity to attach a tenant to: it is spam / free-rider /
# evasive behaviour, and the fraud vertical exists to keep the free tier
# honest. `fraud.disposable_email_domains` EXTENDS (never replaces) the short
# built-in seed in services/disposable_email_block.py — default EMPTY means
# "built-ins only". `fraud.block_disposable_email` is the kill switch,
# registered True in every mode; the gate itself is additionally HOSTED-ONLY
# (services/disposable_email_block._hosted, the same DEPLOYMENT_MODE posture
# FX.108 p1 / FX.74 use for their signup gates), so an on-prem sign-up is
# UNCHANGED unless this switch is explicitly set: an on-prem install has no
# public signup funnel to police, and the owner may legitimately hand out
# throwaway-domain accounts there. Invitations and admin-created
# users are never blocked — those paths never consult these keys.
# ---------------------------------------------------------------------------
register(
    "fraud.block_disposable_email",
    bool,
    True,
    group="gsec",
    description="FX.135 s1 — when true, a self-service sign-up whose email is "
                "on a disposable domain (mailinator, yopmail, 10minutemail, ... "
                "plus the configured list) is refused 422 'Please use a "
                "permanent email address' BEFORE any tenant/user/Keycloak/Stripe "
                "side effect. Default True in every mode; on-prem the gate is "
                "additionally HOSTED-ONLY (services/disposable_email_block._hosted "
                "makes it inert there, the same DEPLOYMENT_MODE posture the FX.108 "
                "age attestation uses), so an on-prem sign-up is UNCHANGED unless "
                "this switch is explicitly set. Invitations and admin-created "
                "users are never blocked. A read failure falls "
                "back to true (the gate fails CLOSED).",
)
register(
    "fraud.disposable_email_domains",
    list,
    [],
    group="gsec",
    description="FX.135 s1 — extra disposable email domains blocked at "
                "self-service sign-up, merged with the built-in seed in "
                "services/disposable_email_block.py (mailinator.com, "
                "guerrillamail.com, 10minutemail.com, tempmail.com, yopmail.com, "
                "trashmail.com, sharklasers.com, getnada.com). Matching is on the "
                "registrable domain plus subdomains (x.mailinator.com), "
                "case-insensitive. Default EMPTY = built-in list only. Never "
                "replaces the seed, so the classics stay blocked.",
)


# api-30.6.14 (2400GX-c14a) — default Retry-After for 429/503 ApiError
register(
    "api.retry_after_default_seconds",
    int,
    30,
    group="api",
    validator=_validate_non_negative_int,
    description="30.6.14 — when ApiError is raised for 429/503 without an "
                "explicit `retry_after`, this DB-backed default (clamp 1..3600) "
                "is used. Read failure falls back to 30. Never set for other "
                "status codes.",
)


# ---------------------------------------------------------------------------
# GSEC FX.120 p5a (owner decision Q107=A) — the SINGLE ACCESS GATEWAY over the
# tailnet + SSH SESSION RECORDING (services/privileged_elevation_gateway.py).
# p2a owns the elevation DATA + state machine, p4a/p4b the per-command exec
# wrapper + its tamper-evident trail; p5a owns the door: production admin access
# happens through ONE gateway, and every elevated session behind it is recorded
# line-by-line INTO THE AUDIT ARCHIVE (redacted text + hash_for_log digest, never
# raw output) and kept for a year. These two keys are the compliance tunables the
# gateway reads; both ship in the COMPLIANT posture (recording ON, retention 365
# days) and a settings-read failure in the gateway degrades to that same posture.
# Retention is SOFT: the gateway reports an older session as expired, it never
# deletes a transcript row (ISO 27001 — removal is a deleted_at stamp elsewhere).
# ---------------------------------------------------------------------------


def _validate_session_retention_days(value) -> None:
    if not isinstance(value, bool) and isinstance(value, int):
        if value < 30 or value > 2555:
            raise ValueError(
                "elevation.session_retention_days must be between 30 and "
                "2555 days (the ISO-permitted retention band), got "
                f"{value!r}")
        return
    raise ValueError(
        f"elevation.session_retention_days must be an int, got {value!r}")


register(
    "elevation.session_recording_enabled",
    bool,
    True,
    restart_required=False,
    group="gsec",
    description="GSEC FX.120 p5a (Q107=A) — SSH session recording behind the "
                "single production-admin access gateway. True (default, the "
                "Q107 posture) = every elevated gateway session is recorded "
                "line-by-line into the audit archive: each row carries the "
                "elevation request id, the actor, the scope, the REDACTED line "
                "(utils.secret_redaction.redact) and output_digest = hash_for_log"
                "(line) — the raw runner output is NEVER stored. False = the "
                "gateway still refuses sessions with no open window but writes "
                "NO transcript rows; that is a documented compliance regression "
                "and the write itself is audited by the normal set_setting path.",
)
register(
    "elevation.session_retention_days",
    int,
    365,
    validator=_validate_session_retention_days,
    restart_required=False,
    group="gsec",
    description="GSEC FX.120 p5a (Q107=A) — how long a recorded gateway session "
                "is kept with the audit archive (days, 30..2555, default 365 = "
                "the Q107 'SSH session recording stays kept for 1 year'). SOFT "
                "only: retention_status() reports a session past the window as "
                "expired; nothing here deletes or ages a row away.",
)

# ---------------------------------------------------------------------------
# GH.5 burst — platform tunables (replaces env switches; owner O5 open)
# ---------------------------------------------------------------------------

register(
    "burst.scale_enabled",
    bool,
    True,
    restart_required=False,
    group="burst",
    description="GH.5 burst scale controller - default ON. Replaces the "
                "BURST_SCALE_ENABLED env switch. When False no burst scale-up is launched. "
                "Default ON means burst-to-cloud scale-up is automatic "
                "for every tenant with a connected cloud burst account.",
)

register(
    "burst.metering_enabled",
    bool,
    True,
    restart_required=False,
    group="burst",
    description="GH.5 burst metering — when ON (default) the periodic "
                "session-debit/teardown tick is registered with the scheduler. "
                "Read at scheduler start (an unreadable value resolves to ON so "
                "teardown still runs). Off: the tick is not registered, so no "
                "debit, drain or zero-balance teardown happens for burst "
                "sessions until it is turned back on and the API restarts.",
)

register(
    "burst.reconciler_enabled",
    bool,
    True,
    restart_required=False,
    group="burst",
    description="GH.5 burst reconciler — when ON (default) the orphan-"
                "instance sweep is registered with the scheduler. Read at "
                "scheduler start (an unreadable value resolves to ON so orphans "
                "are still cleaned up). Off: the sweep is not registered and "
                "orphan cloud instances keep running until it is turned back on "
                "and the API restarts.",
)


def _validate_burst_default_gpu_hours(value) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or value > 100000):
        raise ValueError(
            "burst.default_max_gpu_hours_month must be a finite number "
            f"between 0 and 100000, got {value!r}")


# FIX-CE-UNLIMITED-C (CE has no usage caps). The
# CE sentinel here is NOT 0 — 0 already means "refuse every scale-up" for
# accounts with no own limit, and _validate_burst_default_gpu_hours above
# REJECTS None / float('inf') / -1 (it demands a finite number in 0..100000),
# and services/burst_auto.py:_default_gpu_hours_month re-rejects a
# non-finite read and falls back to the bounded constant. So the only value
# that passes the validator, survives the consumer's finite guard, and means
# "unlimited" in practice is the validator's ceiling 100000.0 (1000x the
# Paid default; CAPS-C item 8 classes 100k h/month as effectively unlimited).
# Paid keeps 100.0 and its old 0 = "refuse everything" meaning; an explicit
# platform/tenant row wins on BOTH editions.
register(
    "burst.default_max_gpu_hours_month",
    float,
    _ce_unlimited(100.0, unlimited=100000.0),
    restart_required=False,
    validator=_validate_burst_default_gpu_hours,
    group="burst",
    description="CLOUD-CE B4 — monthly cloud GPU-hour budget applied to a "
                "burst account whose limits carry no max_gpu_hours_month "
                "(0..100000). 0 refuses every scale-up "
                "for such accounts. The CE default is 100000 = effectively "
                "unlimited (CE has no usage caps; 0 is "
                "taken by 'refuse everything'). An account's own "
                "max_gpu_hours_month always wins; an account whose limits "
                "are present but unreadable is REFUSED (audit "
                "refused_limit_unreadable), never given this default. An "
                "unreadable value here falls back to 100, never to "
                "unlimited.",
)


# Settings that only mean something for paid features register themselves from
# the paid tree (per-stack egress enforcement, stack characterization cadence).
# Absent from the Community Edition tree, so the registry there never lists them.
# Kept at the very end so register() and every validator above already exist.
from utils.paid_import import optional_paid as _optional_paid  # noqa: E402
_optional_paid("services.stack_paid_settings")
