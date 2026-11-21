# --- METADATA BLOCK ---
name = "USD Converter"
description = "Converts USD, USDA, USDC, and USDZ scenes to GLB by extracting mesh geometry with OpenUSD and exporting via trimesh."
node_kind = "usd_convert_process"
category = "Model"
executable_path = "ghcr.io/modtim/usd-converter:latest"
input_schema = {"input_type": "model", "file_mode": "both", "input_variables": "NONE"}
output_schema = {"output_type": "model", "file_mode": "both", "paired": True, "output_variables": "NONE"}
configuration_schema = {"extra_variables": {"TARGET_FORMAT": {"type": "string", "default": "glb", "enum": ["glb"], "description": "Output model format"}}}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
accepted_formats = ["usd", "usda", "usdc", "usdz"]
cost_in_credits = 2
tenant_id = None
image_pull_policy = "always"
container_timeout = 3600
# --- END METADATA BLOCK ---
