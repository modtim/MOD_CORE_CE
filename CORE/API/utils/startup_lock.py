"""B91 (startup-time) — single-flight the lifespan DB-seed task group across
uvicorn workers.

`uvicorn --workers 8` boots EIGHT processes at once and main.py's `lifespan`
runs the SAME ~90-task list in each of them (main.py `_startup_tasks`). Measured
on the 10-12 cold restart (container StartedAt 09:29:31.9Z, healthy ~09:31:18):
16.2 s per worker, and ~10 s of that is the idempotent DB-seeder group — 8x
"register_all_domain_nodes complete: 25 ok", 8x "All system maintenance tasks
completed", 8x "seed_marketplace_catalogue", ... all hammering ONE database
concurrently. The rows they write are identical; seven of the eight passes are
pure waste.

part1 f60067044 (node image-digest backfill, services/node_scanner.py) and part2
0072bfdd7 (platform-unlimited heal, database.py) already single-flight their own
hooks with a Redis `set(nx=True, ex=...)` against the shared REDIS_URL. This
module extends the SAME idiom + the SAME fail-open posture to the rest of the
seed group, so exactly one worker per boot runs the seeders and the other seven
return immediately.

Fail-OPEN is deliberate: no Redis (tests, a degraded deploy, a Redis that has
not started yet) must never disable seeding — seeding is what makes a fresh
install usable at all, so a lock MISS means "proceed", never "skip". Only a
Redis that answered and found the key already held means "skip".

Keys are namespaced `mod:startup:<boot-token>:<name>` so they can never
collide with the existing per-hook keys (`mod:node_digest_backfill:lock`,
`mod:nim_stacks_seed:lock`, `mod:embed_model_seed:lock`, ...) AND so a
`docker restart` (this API restarts often, well within the 1800s TTL) always
gets a brand-new, unheld key: the boot token changes every restart (see
`_boot_token()`), so the previous boot's still-unexpired key is simply a
different key the new boot never looks at. TTL is a safety net, not the
mechanism: a worker that crashes mid-seed can never wedge its OWN boot's key
past the window either.
"""
import logging
import os
import socket

logger = logging.getLogger(__name__)

# Same fallback posture as main.py's embedding-seed lock (main.py:2975) and
# services/node_scanner.py's digest-sweep lock: the pod's Redis, share 2.
DEFAULT_REDIS_URL = "redis://redis:6379/2"

# Namespaced so these boot locks can never shadow the older per-hook keys.
KEY_PREFIX = "mod:startup:"

# Generous: the whole seed group is ~10 s of work. The TTL alone does NOT mean
# "once per boot" — a `docker restart` inside this window would otherwise have
# every worker of the NEW boot see the OLD boot's still-unexpired key and skip
# seeding entirely (the API restarts often). The key is therefore scoped to the
# boot via `_boot_token()` below, so a new boot always starts with a fresh key
# regardless of TTL; this EX is just a safety net against a holder that dies
# mid-seed without ever clearing its own slot.
DEFAULT_EX_S = 1800

_BOOT_TOKEN_CACHE = None


def _read_boot_token() -> str:
    """Identify "this boot": the container's hostname plus PID 1's start time
    (`/proc/1/stat` field 22, in clock ticks since system boot). All 8 uvicorn
    workers of one `docker restart my_fastapi_app` share the same PID
    namespace, so they all read the SAME PID 1 and compute the SAME token —
    no coordination needed. The next `docker restart` replaces PID 1 with a
    new process that has a new start time, so the token changes and every
    lock key changes with it, even though the hostname (container identity)
    did not. Non-Linux / sandboxed callers (local dev, some test runners) fall
    back to hostname alone — still correct on container *recreate*, just not
    on an in-place restart, which is the Linux/Docker production case this
    exists for.
    """
    try:
        with open("/proc/1/stat") as f:
            stat = f.read()
        # Fields after the comm field's closing ")" are space-separated and
        # start at field 3 (state); field 22 (starttime) is index 19 there.
        starttime = stat.rsplit(")", 1)[-1].split()[19]
        return f"{socket.gethostname()}-{starttime}"
    except Exception:
        return socket.gethostname()


def _boot_token() -> str:
    """Cached per-process; reset via module attribute for tests that need to
    simulate a second boot without starting a second process."""
    global _BOOT_TOKEN_CACHE
    if _BOOT_TOKEN_CACHE is None:
        _BOOT_TOKEN_CACHE = _read_boot_token()
    return _BOOT_TOKEN_CACHE

# The pure-DB seed group main.py's `lifespan` single-flights (lives here so the
# policy has ONE source of truth: main.py imports this set, tests assert it).
# Every entry is an idempotent, insert-only seeder/healer — a worker that skips
# still serves, because the lock holder is writing the same rows and the
# scheduler jobs re-seed anything left undone.
#
# Order WITHIN the group is NOT defined here: it is defined by main.py's
# `_startup_tasks` list (a workflow seeder must follow the nodes it references,
# `heal_system_workflow_nodes` runs last of the group). Locking per task name
# keeps that relative order — the holder runs them in list order, the others
# skip each one as they reach it.
#
# DELIBERATELY ABSENT (order-critical, must run in EVERY worker): migrations,
# Vault/DB init, tenant creation (`ensure_default_tenant` /
# `ensure_free_tier_plan`), `verify_email_sync`, `push_realm_session_tier`,
# `audit_hmac`, `seed_platform_settings`.
SEED_GROUP_SINGLE_FLIGHT_TASKS = frozenset({
    "seed_embedded_worker_enrollment",
    "audit_reserved_secret_collisions",
    "seed_capabilities",
    "seed_converters",
    "seed_domain_nodes",
    "seed_human_approval_node",
    "seed_build_dag_system_nodes",
    "seed_asr_nim_node",
    "seed_harbor_scan_followup_node",
    "seed_harbor_scan_followup_workflow",
    "seed_task_extract_node",
    "seed_task_merge_node",
    "seed_plane_node",
    "seed_talk_to_task_workflow",
    "seed_talk_to_task_stack",
    "seed_web_search_node",
    "seed_web_search_workflow",
    "seed_nim_stacks",
    "seed_nim_characterizations",
    "seed_marketplace_catalogue",
    # 1900Y3b — CE boot never runs migrations, so the 4 curated WorkflowTemplate
    # rows (migrations/20260425_seed_workflow_templates.py) that the WB "Templates"
    # gallery lists never land on a fresh CE DB. Insert-only + idempotent.
    "seed_workflow_templates",
    # 1795 review — ensure_starter_package's "insert if missing" NodePackage
    # lookup has no unique constraint on (slug, tenant_id); 8 concurrent
    # uvicorn boot workers racing it could create duplicate "mod-core" rows.
    "starter_package_sync",
    "heal_system_workflow_nodes",
})


def startup_lock_key(name: str) -> str:
    """The Redis key for one startup task, scoped to the current boot so a
    restart always starts with a fresh, unheld key regardless of the previous
    boot's TTL."""
    return f"{KEY_PREFIX}{_boot_token()}:{name}"


def startup_single_flight(name, ex=DEFAULT_EX_S) -> bool:
    """True when THIS process won the cross-process slot for startup task `name`.

    One `set(nx=True, ex=...)` on `mod:startup:<name>`: the first worker to ask
    gets the key and runs the task; every other worker sees the key already held
    and returns False (the caller logs "another worker holds the lock; skipping"
    and returns without touching the DB).

    Never raises. If Redis is unreachable/down we PROCEED (fail-open) — seeding
    must not depend on Redis, and the seeders are all idempotent insert-only so
    an overlap is at worst wasted work, never a lost row.
    """
    try:
        import redis as _redis
        client = _redis.Redis.from_url(os.getenv("REDIS_URL", DEFAULT_REDIS_URL))
        if not client.set(startup_lock_key(name), "1", nx=True, ex=ex):
            logger.info(
                "%s: another worker holds the lock; skipping", name
            )
            return False
        return True
    except Exception as e:  # noqa: BLE001 — lock is best-effort
        logger.warning(
            "%s: lock unavailable (%s); proceeding", name, e
        )
        return True
