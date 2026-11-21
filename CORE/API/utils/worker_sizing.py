"""Size the uvicorn worker pool to the machine it actually booted on.

The worker count was hardcoded at 20. That number was chosen for one specific
box and is wrong everywhere else: on a small VM it oversubscribes CPU and
starves co-tenants, and on the production server — twice this box's size and
carrying far more traffic — it leaves capacity unused. Neither failure is
visible as an error; both present as latency.

Three independent ceilings bound how many workers a deployment can carry, and
the honest answer is the SMALLEST of them. Sizing on CPU alone is the usual
mistake, because CPU is rarely the thing that breaks first.

    CPU     each uvicorn worker is a single-threaded event loop, so the
            natural target is one worker per core available to this service.

    MEMORY  every worker is a full copy of the app — SQLAlchemy, numpy,
            onnxruntime. "Full copy" is literal: uvicorn SPAWNS workers rather
            than forking, so there is no copy-on-write sharing of the import
            graph and each worker pays the whole cost again. Verified
            2026-08-16 by comparing PSS against RSS per worker — they are
            within 10% of each other, which is what no sharing looks like.

            Re-measured on this deployment 2026-08-16: ~400 MB at rest for a
            plain worker, but ~870 MB for the two running the background
            threads (scheduler, warmups). The earlier ~330 MB figure predates
            those. The default below is the AVERAGE across the pool, because
            that is what the ceiling arithmetic needs; sizing on the plain
            worker would under-count every deployment by the difference.

    DATABASE  the ceiling nobody had encoded, and the one that was already
            being exceeded. Each worker holds a DIRECT engine for the
            session-pinned LISTEN/NOTIFY listener (database.py, pool_size=1 +
            max_overflow=2), bypassing PgBouncer entirely — three real Postgres
            connections per worker. Those sit ALONGSIDE PgBouncer's own server
            backends, so the budget is:

                workers x direct_pool  +  pgbouncer_max_db  <=  max_connections

            At the old hardcoded 20 against max_connections=200 that read
            20*3 + 150 = 210 > 197, i.e. over budget. It survived only because
            those pools never all filled at once. Exceeding it for real is not
            graceful degradation: it is `FATAL: sorry, too many clients`, and
            the API stops serving.

The result is logged with the BINDING constraint named, because "12 workers" on
its own tells an operator nothing about what to change to get more.

Everything is overridable. `API_UVICORN_WORKERS` short-circuits the whole
calculation for the case where someone knows better than the formula.
"""

import math
import os
from typing import Dict, NamedTuple, Optional, Tuple

CGROUP_V2_CPU = "/sys/fs/cgroup/cpu.max"
CGROUP_V2_MEM = "/sys/fs/cgroup/memory.max"
CGROUP_V1_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
CGROUP_V1_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
CGROUP_V1_MEM = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
MEMINFO = "/proc/meminfo"

# A cgroup v1 "no limit" is a sentinel close to 2^63, not a zero or a flag.
# Anything this large is the kernel saying "unlimited", not a real 8-exabyte
# allowance, and treating it literally would make the memory ceiling infinite.
_V1_UNLIMITED = 1 << 62


class Probe(NamedTuple):
    """What the machine reports about itself.

    Separated from the arithmetic so the decision can be tested against a small
    VM, a huge server, and both cgroup versions without needing any of them.
    """
    cpus: float
    memory_bytes: int
    cpu_is_limit: bool
    memory_is_limit: bool


class Sizing(NamedTuple):
    workers: int
    binding: str
    ceilings: Dict[str, int]
    detail: str


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.getenv(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ============================================================================
# Deployment role (scale-tiered-workloads D1 + D3)
# ============================================================================
# One image, two roles, selected by MOD_API_ROLE:
#
#   web         serves HTTP (uvicorn + routes). Stateless, horizontally
#               scalable. Runs NO background loops once the split is fully
#               rolled out.
#   background  runs the scheduler + orchestration queue consumer + ML/ingest
#               loops. Not web-facing (the entrypoint runs a worker process,
#               not uvicorn — a health port only, at most).
#
# The catch (D1/D3 rollout, tasks §2.1 + §3): until a `background` sibling is
# actually deployed, a lone `web` container must keep doing EVERYTHING or a
# single-container deploy would silently stop scheduling/orchestrating. So the
# gate is deliberately conservative:
#
#     should_run_background_work() == (role == "background")
#                                     OR (role == "web" AND single-process)
#
# and MOD_SINGLE_PROCESS defaults True. Net effect: a default deploy (no env
# changes at all — role defaults to "web", single-process defaults True) runs
# exactly as it does today. Only once an operator deploys a background sibling
# AND sets MOD_SINGLE_PROCESS=0 on the web tier does web stop running the
# background work. This is why nothing here changes behaviour on its own.
# ============================================================================

DEFAULT_API_ROLE = "web"
_VALID_ROLES = ("web", "background")


def get_api_role() -> str:
    """The role THIS process was started as. Unknown/empty -> the safe default
    (`web`), so a typo can never accidentally silence the web tier."""
    role = (os.getenv("MOD_API_ROLE", "") or "").strip().lower()
    return role if role in _VALID_ROLES else DEFAULT_API_ROLE


def is_web_role() -> bool:
    return get_api_role() == "web"


def is_background_role() -> bool:
    return get_api_role() == "background"


def single_process_mode() -> bool:
    """Whether a lone process still carries the background work.

    Defaults True so the current single-container deployment is unchanged; an
    operator flips it to 0 on the `web` tier once a `background` sibling exists.
    """
    return _env_bool("MOD_SINGLE_PROCESS", True)


def should_run_background_work() -> bool:
    """D3: does THIS process run the scheduler / ML / ingest / orchestration
    consumer? Background always does; web does only while single-process."""
    if is_background_role():
        return True
    return is_web_role() and single_process_mode()


def should_serve_http() -> bool:
    """D1: only the `web` role serves the HTTP request path via uvicorn."""
    return is_web_role()


def probe_cpus() -> Tuple[float, bool]:
    """Cores available to THIS service, and whether that came from a limit.

    A cgroup quota is preferred over the host core count whenever one is set:
    if an operator has capped the container, that cap IS this service's share
    and no further guessing is warranted. Absent a cap, the caller applies a
    share factor instead — see `compute`.
    """
    raw = _read(CGROUP_V2_CPU)
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = float(parts[0]), float(parts[1])
                if quota > 0 and period > 0:
                    return quota / period, True
            except ValueError:
                pass

    quota_raw, period_raw = _read(CGROUP_V1_QUOTA), _read(CGROUP_V1_PERIOD)
    if quota_raw and period_raw:
        try:
            quota, period = float(quota_raw), float(period_raw)
            if quota > 0 and period > 0:
                return quota / period, True
        except ValueError:
            pass

    # sched_getaffinity, not cpu_count: it reflects the CPUs this process may
    # actually run on, which is what a taskset or a cpuset cgroup constrains.
    try:
        return float(len(os.sched_getaffinity(0))), False
    except (AttributeError, OSError):
        return float(os.cpu_count() or 1), False


def probe_memory() -> Tuple[int, bool]:
    """Bytes available to THIS service, and whether that came from a limit."""
    raw = _read(CGROUP_V2_MEM)
    if raw and raw != "max":
        try:
            v = int(raw)
            if 0 < v < _V1_UNLIMITED:
                return v, True
        except ValueError:
            pass

    raw = _read(CGROUP_V1_MEM)
    if raw:
        try:
            v = int(raw)
            if 0 < v < _V1_UNLIMITED:
                return v, True
        except ValueError:
            pass

    # MemTotal rather than MemAvailable on purpose. MemAvailable swings with
    # whatever else happens to be running at boot, so sizing on it would give a
    # different worker count on every restart — and a restart during a busy
    # period would permanently shrink the pool that was meant to serve it.
    meminfo = _read(MEMINFO) or ""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(line.split()[1]) * 1024, False
            except (IndexError, ValueError):
                break
    return 0, False


def probe() -> Probe:
    cpus, cpu_is_limit = probe_cpus()
    memory, mem_is_limit = probe_memory()
    return Probe(cpus=cpus, memory_bytes=memory,
                 cpu_is_limit=cpu_is_limit, memory_is_limit=mem_is_limit)


def compute(p: Probe) -> Sizing:
    """Turn a probe into a worker count, and say which ceiling bound it.

    The share factors apply ONLY when the probe fell back to host-wide numbers.
    This API shares its host with ~30 other containers, so consuming the whole
    machine's core count would be a claim on capacity it does not have. When a
    cgroup limit is present that limit already expresses the share, and
    multiplying it again would silently under-size the service.
    """
    cpu_share = 1.0 if p.cpu_is_limit else _env_float("API_CPU_SHARE", 0.4)
    mem_share = 1.0 if p.memory_is_limit else _env_float("API_MEM_SHARE", 0.4)

    # One worker per core: a worker is a single event loop, so a second worker
    # on the same core buys nothing but memory. Blocking calls inside async
    # handlers are a real effect, but the fix for those is threadpool sizing,
    # not oversubscribing the CPU.
    per_core = _env_float("API_WORKERS_PER_CORE", 1.0)
    cpu_ceiling = max(1, int(math.floor(p.cpus * cpu_share * per_core)))

    # 500, not 400: measured average across the pool on 2026-08-16 (see module
    # docstring). Raising it LOWERS the memory ceiling, so this can only
    # shrink a pool on a memory-bound host — the safe direction to be wrong in,
    # since the failure it avoids is the OOM killer taking the API out.
    #
    # scale-tiered-workloads §2.3 (D1): that 500 is the pool AVERAGE precisely
    # BECAUSE some workers also carry the background threads (~870MB each vs a
    # ~400MB plain worker). Once the background role owns that work, the web
    # workers are all plain workers, so sizing them at the average over-counts
    # memory and needlessly caps the pool. When this process is NOT carrying
    # background work (a web tier with a deployed background sibling), size on
    # the leaner plain-worker figure so the web tier scales for HTTP
    # concurrency, not for threads it no longer runs. Default single-process
    # deploy still hits the 500 branch, so its sizing is unchanged.
    if should_run_background_work():
        worker_mb = _env_int("API_WORKER_MEM_MB", 500)
    else:
        worker_mb = _env_int("API_WEB_WORKER_MEM_MB", 400)
    reserve_mb = _env_int("API_WORKER_MEM_RESERVE_MB", 512)
    usable_mb = int((p.memory_bytes / (1024 * 1024)) * mem_share) - reserve_mb
    mem_ceiling = max(1, usable_mb // worker_mb) if usable_mb > 0 else 1

    # See the module docstring. PgBouncer multiplexes the pooled engine, but the
    # direct engine does not go through it, so its cost is strictly per worker.
    pg_max = _env_int("PG_MAX_CONNECTIONS", 300)
    pg_reserved = _env_int("PG_RESERVED_CONNECTIONS", 3)
    bouncer = _env_int("PGBOUNCER_MAX_DB_CONNECTIONS", 150)
    direct_pool = _env_int("API_DIRECT_POOL_MAX", 3)
    db_budget = pg_max - pg_reserved - bouncer
    db_ceiling = max(1, db_budget // direct_pool) if db_budget > 0 else 1

    ceilings = {"cpu": cpu_ceiling, "memory": mem_ceiling, "database": db_ceiling}
    binding = min(ceilings, key=lambda k: ceilings[k])

    floor_w = _env_int("API_UVICORN_WORKERS_MIN", 2)
    cap_w = _env_int("API_UVICORN_WORKERS_MAX", 32)
    workers = max(floor_w, min(ceilings[binding], cap_w))

    if workers != ceilings[binding]:
        # Say so explicitly. A clamp that silently overrides the computed value
        # is how a box ends up mis-sized with a log line that looks correct.
        binding = f"{binding} (clamped to [{floor_w}, {cap_w}])"

    detail = (
        f"cpus={p.cpus:g}{' (limit)' if p.cpu_is_limit else f' x{cpu_share:g} share'}, "
        f"mem={p.memory_bytes / (1024 ** 3):.1f}GiB"
        f"{' (limit)' if p.memory_is_limit else f' x{mem_share:g} share'}, "
        f"db_budget={db_budget}/{direct_pool} per worker"
    )
    return Sizing(workers=workers, binding=binding, ceilings=ceilings, detail=detail)


def decide() -> Sizing:
    """The full decision, including the operator override."""
    override = os.getenv("API_UVICORN_WORKERS", "").strip()
    if override:
        try:
            n = int(override)
            if n > 0:
                return Sizing(workers=n, binding="API_UVICORN_WORKERS override",
                              ceilings={}, detail="formula not consulted")
        except ValueError:
            pass
    return compute(probe())


def main() -> None:
    """Print the worker count on stdout; the reasoning on stderr.

    Split so the entrypoint can consume the number directly while the
    explanation still reaches the container log, where an operator wondering
    why the API is slow will actually look.
    """
    import sys
    s = decide()
    if s.ceilings:
        ceil_txt = ", ".join(f"{k} {v}" for k, v in s.ceilings.items())
        print(f"--> uvicorn workers: {s.workers} "
              f"(ceilings: {ceil_txt}; binding: {s.binding})", file=sys.stderr)
        print(f"    {s.detail}", file=sys.stderr)
    else:
        print(f"--> uvicorn workers: {s.workers} ({s.binding})", file=sys.stderr)
    print(s.workers)


if __name__ == "__main__":
    main()
