# --- METADATA BLOCK ---
# TEMPLATE: Copy this file and replace all values marked with [REPLACE]
name = "[REPLACE] Your Process Name"  # e.g., "Roughness Texture Process", "Normal Map Generator"
description = "[REPLACE] Process - Description of what this processor does"
node_kind = "[REPLACE]_process"  # e.g., "roughness_texture_process", "normal_map_generator" (lowercase, underscores)
category = "[REPLACE]"  # e.g., "Texture", "Model", "Video"
# Note: executable_path will be constructed from DOCKER_REGISTRY env var at runtime
executable_path = "[REPLACE]:latest"  # e.g., "roughness:latest", "normal-map:latest" - Registry prefix added at runtime
input_schema = {"input_type": "[REPLACE]", "file_mode": "both", "input_variables": "NONE"}  # e.g., "image", "video", "model"
output_schema = {"output_type": "[REPLACE]", "file_mode": "both", "paired": True, "output_variables": "NONE"}  # e.g., "image", "video"
configuration_schema = {"extra_variables": "NONE"}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"  # Each item processed independently (1 input -> 1 output)
cost_in_credits = 2  # [REPLACE] Adjust cost based on processing intensity
tenant_id = None
image_pull_policy = "always"  # Options: "always" (dev), "if_not_present" (prod), "never" (air-gapped)
# Container resource limits and timeouts
container_timeout = 7200  # [REPLACE] Adjust based on typical processing time (seconds)
# --- END METADATA BLOCK ---
