"""Image-name helper used by the API when building task payloads.

`worker_tasks.py` uses `get_full_image_name` to build image references
for the Go worker when enqueuing ApiTaskQueue rows.
"""

from utils.registries import platform_image, platform_project


def get_full_image_name(executable_path: str, registry: str = None) -> str:
    """
    Build full image name from executable_path and registry.

    Delegates to utils.registries so the platform ref honours DOCKER_REGISTRY,
    HARBOR_PROJECT and the CE_IMAGE_REGISTRY fallback instead of a hard-coded
    host/project. Already-qualified refs pass through untouched.

    Args:
        executable_path: Image name from node metadata (e.g., "albedo:latest")
        registry: Explicit registry host/project prefix; when omitted the
                  platform resolvers decide both halves.

    Returns:
        Full image name (e.g., "ghcr.io/modtim/albedo:latest")

    Example:
        >>> get_full_image_name("albedo:latest")
        "ghcr.io/modtim/albedo:latest"
    """
    if registry:
        return f"{registry}/{platform_project()}/{executable_path}"
    return platform_image(executable_path)
