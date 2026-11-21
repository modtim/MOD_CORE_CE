"""
Benchmark strategy generator — dispatches an LLM task when the system
encounters an accelerator kind it has no curated strategy for.

Orchestration flow:
  1. resolve_or_generate() called from _auto_dispatch_profiling when no
     strategy matches (kind, vendor, device).
  2. Builds a structured prompt describing the device + runtime env.
  3. Enqueues an `llm_generate_strategy` task on the CPU queue using the
     same system-task pattern as trivy scans and benchmarks.
  4. When the task completes, the result callback validates the generated
     script, persists it to accelerator_benchmark_strategies with
     source='llm_generated' and auto_approved=false, and returns the
     strategy id.

The actual LLM inference runs inside the mod-llm-runner container (Ollama
+ baked-in model). This module never calls an LLM directly — it talks
to the internal task queue and lets the worker fleet handle the compute.
"""

import json
import logging
import os
import re
from typing import Optional

from sqlalchemy.orm import Session

from models import (
    AcceleratorBenchmarkStrategy,
    ApiTaskQueue,
    ApiTaskStatus,
)

logger = logging.getLogger(__name__)

# Output schema the LLM must produce. Validated before persisting.
EXPECTED_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["script", "notes"],
    "properties": {
        "script": {"type": "string", "minLength": 10, "maxLength": 16384},
        "env": {"type": "object"},
        "notes": {"type": "string"},
    },
}

PROMPT_TEMPLATE = """You are generating a benchmark test module for a new hardware accelerator.
The system detected a device it has no curated benchmark for.

Device info:
  Kind: {kind}
  Vendor ID: {vendor_id}
  Device ID: {device_id}
  Name: {name}
  Driver Version: {driver_version}
  Available runtimes on the worker: {runtimes}
  Host OS: linux

You must implement a Python module with exactly these 3 functions:

    def get_device_env_var() -> tuple:
        \"\"\"Returns (env_var_name, value) to isolate TARGET_INDEX device.\"\"\"

    def run_compute_test() -> float:
        \"\"\"Matrix multiply or equivalent. Return GFLOPS.\"\"\"

    def run_memory_bw_test() -> float:
        \"\"\"Device-local memory bandwidth. Return GB/s.\"\"\"

    def run_copy_bw_test() -> float:
        \"\"\"Host-to-device transfer. Return GB/s.\"\"\"

The common entrypoint (which you do NOT write) calls these 3 functions,
outputs JSON, and handles HMAC signing. You ONLY write the test module.

Here are two WORKING examples from our curated benchmarks:

=== EXAMPLE 1: CUDA (tests/cuda.py) ===
{cuda_example}

=== EXAMPLE 2: ROCm (tests/rocm.py) ===
{rocm_example}

Now generate a test module for the device described above.
Follow the SAME pattern: import the appropriate SDK, set the device
visibility env var, and implement the 3 functions. Use try/except to
fall back gracefully if the SDK isn't available.

Constraints:
- Python 3.12 compatible.
- Must reference os.environ["TARGET_INDEX"].
- Must NOT touch the network, call rm/curl/wget/sudo.
- Must finish within 120 seconds.
- If the device is not accessible, return 0.0 from each function.

Respond with ONLY a JSON object (no markdown, no explanation):
{{"script": "<python source code>", "env": {{}}, "notes": "one-liner for operator review"}}
"""


def _load_example_modules():
    """Load the curated CUDA and ROCm test modules as few-shot examples."""
    examples = {}
    for kind, path in [
        ("cuda", os.path.join(os.path.dirname(__file__), "..", "..", "DockerContainers",
                              "accelerator-benchmark-cuda", "tests", "cuda.py")),
        ("rocm", os.path.join(os.path.dirname(__file__), "..", "..", "DockerContainers",
                              "accelerator-benchmark-rocm", "tests", "rocm.py")),
    ]:
        try:
            with open(path) as f:
                examples[kind] = f.read()
        except FileNotFoundError:
            examples[kind] = f"# {kind}.py not found at {path}"
    return examples


def build_prompt(kind: str, vendor_id: str, device_id: str, name: str,
                 driver_version: str, runtimes: list) -> str:
    examples = _load_example_modules()
    return PROMPT_TEMPLATE.format(
        kind=kind or "unknown",
        vendor_id=vendor_id or "unknown",
        device_id=device_id or "unknown",
        name=name or "unknown",
        driver_version=driver_version or "unknown",
        runtimes=", ".join(runtimes) if runtimes else "none detected",
        cuda_example=examples.get("cuda", "# not available"),
        rocm_example=examples.get("rocm", "# not available"),
    )


def dispatch_strategy_generation(
    db: Session,
    kind: str,
    vendor_id: Optional[str],
    device_id: Optional[str],
    name: Optional[str],
    driver_version: Optional[str],
    runtimes: Optional[list] = None,
    tenant_id: int = 1,
) -> int:
    """
    Enqueue an llm_generate_strategy task. Returns the task ID.

    The task runs the mod-llm-runner container which reads the prompt from
    env, runs Ollama, and writes JSON to stdout. When it completes,
    _process_llm_strategy_result() is called to validate and persist.
    """
    from utils.registries import platform_registry
    registry = platform_registry()
    prompt = build_prompt(kind, vendor_id, device_id, name, driver_version, runtimes)

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"llm-strategy-{kind}-{vendor_id or 'unknown'}",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=7,
        task_type="system",
        task_payload={
            "node_kind": "llm_generate_strategy",
            "processing_image": f"{registry}/modtim/mod-llm-runner-granite-8b-cpu:latest",
            "container_env": {
                "PROMPT": prompt,
                "MAX_TOKENS": "4096",
                "TEMPERATURE": "0.2",
                "TARGET_KIND": kind,
                "VENDOR_ID": vendor_id or "",
                "DEVICE_ID": device_id or "",
            },
            "source_file_location": "",
            "gpu_required": False,
            "container_timeout": 300,
            "skip_download": True,
            "skip_upload": True,
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=2,
        output_namespace=f"llm_strategy_{kind}",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    logger.info("Dispatched LLM strategy generation for kind=%s vendor=%s task=%d", kind, vendor_id, task.id)
    return task.id


def validate_and_persist_strategy(
    db: Session,
    kind: str,
    vendor_id: Optional[str],
    device_id: Optional[str],
    log_tail: str,
) -> Optional[AcceleratorBenchmarkStrategy]:
    """
    Parse the LLM's output from log_tail, validate the script, and persist
    as a new AcceleratorBenchmarkStrategy with source='llm_generated'.

    Returns the strategy row or None if validation failed.
    """
    if not log_tail:
        logger.warning("LLM strategy generation returned empty log_tail")
        return None

    # Parse the leading JSON (same raw_decode pattern as benchmark results)
    try:
        decoder = json.JSONDecoder()
        parsed, _ = decoder.raw_decode(log_tail.lstrip())
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("LLM strategy output is not valid JSON: %s", e)
        return None

    script = parsed.get("script", "")
    env_template = parsed.get("env", {})
    notes = parsed.get("notes", "")

    # Validate script
    if not script or len(script) < 10:
        logger.error("LLM strategy script is too short (%d chars)", len(script))
        return None
    if len(script) > 16384:
        logger.error("LLM strategy script is too long (%d chars)", len(script))
        return None

    # Safety checks
    dangerous_patterns = [
        r"os\.system\s*\(.*\brm\b",
        r"subprocess.*\b(curl|wget|rm|sudo)\b",
        r"shutil\.rmtree",
        r"__import__\s*\(",
    ]
    for pattern in dangerous_patterns:
        if re.search(pattern, script, re.IGNORECASE):
            logger.error("LLM strategy script contains dangerous pattern: %s", pattern)
            return None

    if "TARGET_INDEX" not in script:
        logger.error("LLM strategy script does not reference TARGET_INDEX")
        return None

    from utils.registries import platform_registry
    registry = platform_registry()
    image_ref = f"{registry}/modtim/accelerator-benchmark-generic:latest"

    # iso-soft-delete-sweep (2026-09-22): AcceleratorBenchmarkStrategy carries
    # SoftDeleteMixin (rejected strategies are soft-deleted in
    # crud/accelerators.py, not hard-removed) and is in database.py's
    # soft-delete listener. `uq_strategy_kind_vendor_device`
    # (kind, vendor_id, device_id_pattern) is a plain UNIQUE, so re-benchmarking
    # a device whose prior strategy was rejected (now a tombstone) would collide
    # on INSERT. Look the row up WITH tombstones and upsert/reactivate in place.
    strategy = (
        db.query(AcceleratorBenchmarkStrategy)
        .execution_options(include_deleted=True)
        .filter(
            AcceleratorBenchmarkStrategy.kind == kind,
            AcceleratorBenchmarkStrategy.vendor_id == vendor_id,
            AcceleratorBenchmarkStrategy.device_id_pattern == device_id,
        )
        .first()
    )
    if strategy is not None:
        strategy.deleted_at = None
        strategy.image_ref = image_ref
        strategy.script_content = script
        strategy.env_template = env_template
        strategy.source = "llm_generated"
        strategy.auto_approved = False
        strategy.notes = notes
    else:
        strategy = AcceleratorBenchmarkStrategy(
            kind=kind,
            vendor_id=vendor_id,
            device_id_pattern=device_id,
            image_ref=image_ref,
            script_content=script,
            env_template=env_template,
            source="llm_generated",
            auto_approved=False,
            notes=notes,
        )
        db.add(strategy)
    db.commit()
    db.refresh(strategy)

    logger.info(
        "LLM-generated strategy persisted: id=%d kind=%s vendor=%s notes=%s",
        strategy.id, kind, vendor_id, notes[:100],
    )
    return strategy
