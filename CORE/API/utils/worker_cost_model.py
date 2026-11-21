"""
Worker readiness cost model.

Estimates the total time + resource cost for a worker to complete a task,
including all warmup phases: boot, image pull, data download, processing.

Used by:
- check_remote_availability() to rank workers by total ETA
- predictive_scheduling.py to offset resource curves by warmup time
- Traffic_Cop_Dag to decide between local vs remote dispatch
"""

import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


def estimate_image_pull_time(
    image_name: str,
    worker_hardware_specs: Dict[str, Any],
    historical_pull_durations: Optional[List[float]] = None,
) -> float:
    """
    Predict how long an image pull will take on this worker.

    Priority:
    1. Historical pull durations for this image on similar hardware
    2. Image size / measured throughput
    3. Image size / NIC speed * safety factor
    4. Safe default (30s)
    """
    if historical_pull_durations:
        return sum(historical_pull_durations) / len(historical_pull_durations)

    # Use measured throughput if available
    network = worker_hardware_specs.get("network", {})
    throughput_mbps = network.get("minio_throughput_mbps", 0)

    if throughput_mbps > 0:
        # Assume average Docker image ~500MB compressed, adjust with safety factor
        estimated_image_mb = 500
        return (estimated_image_mb * 8) / throughput_mbps * 1.2  # 20% overhead for decompression

    # Fall back to NIC speed with larger safety factor
    nic_speed_mbps = worker_hardware_specs.get("network_mbps", 0)
    if nic_speed_mbps > 0:
        estimated_image_mb = 500
        return (estimated_image_mb * 8) / nic_speed_mbps * 2.0  # 2x safety for NIC vs real throughput

    return 30.0  # Safe default


def estimate_transfer_time(
    file_size_mb: float,
    worker_hardware_specs: Dict[str, Any],
) -> float:
    """
    Estimate data download time from MinIO to this worker.
    """
    if file_size_mb <= 0:
        return 0.0

    network = worker_hardware_specs.get("network", {})
    throughput_mbps = network.get("minio_throughput_mbps", 0)

    if throughput_mbps > 0:
        return (file_size_mb * 8) / throughput_mbps

    nic_speed_mbps = worker_hardware_specs.get("network_mbps", 0)
    if nic_speed_mbps > 0:
        return (file_size_mb * 8) / nic_speed_mbps * 1.5

    # Conservative default: 100 Mbps
    return (file_size_mb * 8) / 100


def estimate_task_readiness_cost(
    worker_status: str,
    worker_hardware_specs: Dict[str, Any],
    worker_cached_images: List[str],
    task_payload: Dict[str, Any],
    processing_image: Optional[str] = None,
    historical_durations: Optional[List[float]] = None,
    historical_pull_durations: Optional[List[float]] = None,
    file_size_mb: float = 0.0,
) -> Dict[str, Any]:
    """
    Full time + resource cost for a worker to complete a task, including warmup.

    Args:
        worker_status: "ONLINE", "OFFLINE", etc.
        worker_hardware_specs: hardware_specs JSONB from WorkerLicense
        worker_cached_images: List of Docker images cached on this worker
        task_payload: Full task payload from ApiTaskQueue
        processing_image: Docker image needed (resolved from task_payload if not provided)
        historical_durations: Past processing durations for this node_kind
        historical_pull_durations: Past image pull durations for this image
        file_size_mb: Input data size in MB

    Returns:
        Dict with boot_seconds, image_pull_seconds, input_download_seconds,
        processing_seconds, total_eta_seconds, confidence, and resource impact.
    """
    # Boot time (0 if worker is already online)
    boot_seconds = 0.0
    if worker_status != "LICENSED" and worker_status != "ONLINE":
        boot_seconds = worker_hardware_specs.get("boot_duration_seconds", 60.0)

    # Image pull time (0 if image is cached)
    image = processing_image or task_payload.get("processing_image", "")
    image_pull_seconds = 0.0
    image_cached = image in worker_cached_images if image else True
    if not image_cached:
        image_pull_seconds = estimate_image_pull_time(
            image, worker_hardware_specs, historical_pull_durations
        )

    # Input download time
    input_download_seconds = estimate_transfer_time(file_size_mb, worker_hardware_specs)

    # Processing time from historical data
    processing_seconds = 0.0
    confidence = 0.3  # Base confidence (no history)
    if historical_durations:
        processing_seconds = sum(historical_durations) / len(historical_durations)
        sample_count = len(historical_durations)
        # Confidence scales with sample count: 0.5 at 1 sample, 0.95 at 50+
        confidence = min(0.95, 0.5 + (sample_count / 100))
    else:
        # No history — use a conservative default based on queue type
        processing_seconds = 120.0  # 2 minutes default
        confidence = 0.1

    total_eta = boot_seconds + image_pull_seconds + input_download_seconds + processing_seconds

    # Resource impact of warmup phases on concurrent tasks
    network = worker_hardware_specs.get("network", {})
    throughput = network.get("minio_throughput_mbps", 0)
    nic_speed = worker_hardware_specs.get("network_mbps", 1000)  # Default 1Gbps
    effective_bandwidth = throughput or (nic_speed * 0.5)

    # During warmup, image pull + data download consume network
    warmup_network_mbps = 0.0
    if not image_cached and effective_bandwidth > 0:
        warmup_network_mbps += effective_bandwidth * 0.5  # Pull uses ~50% bandwidth
    if file_size_mb > 0 and effective_bandwidth > 0:
        warmup_network_mbps += effective_bandwidth * 0.3  # Download uses ~30%

    network_saturation = min(1.0, warmup_network_mbps / effective_bandwidth) if effective_bandwidth > 0 else 0

    return {
        "boot_seconds": boot_seconds,
        "image_pull_seconds": image_pull_seconds,
        "image_pull_resources": {
            "network_mbps": warmup_network_mbps * 0.5 if not image_cached else 0,
            "disk_io_mbps": 50.0 if not image_cached else 0,  # Estimated write for image layers
            "cpu_cores": 0.5 if not image_cached else 0,  # Decompression
            "ram_mb": 200 if not image_cached else 0,
        },
        "input_download_seconds": input_download_seconds,
        "processing_seconds": processing_seconds,
        "total_eta_seconds": total_eta,
        "confidence": confidence,
        "warmup_resource_impact": {
            "network_saturation_pct": round(network_saturation * 100, 1),
            "disk_saturation_pct": 10.0 if not image_cached else 0,  # Conservative estimate
        },
    }
