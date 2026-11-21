# --- METADATA BLOCK ---
name = "Image Input"
description = "Input - Image file input and validation"
node_kind = "image_input"
category = "Input"
executable_path = "ghcr.io/modtim/image-input:latest"
image_pull_policy = "IfNotPresent"
container_timeout = 300
container_gpu_required = False
input_schema = {"input_type": "NONE", "input_variables": "NONE"}
output_schema = {"output_type": "image", "file_mode": "both", "output_variables": "NONE"}
configuration_schema = {"extra_variables": "NONE"}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
cost_in_credits = 0
tenant_id = None
# --- END METADATA BLOCK ---
