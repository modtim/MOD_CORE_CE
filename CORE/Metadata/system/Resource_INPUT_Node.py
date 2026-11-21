# --- METADATA BLOCK ---
name = "Resource Input"
description = "Input - Named file resource for shaders, configs, blend files, or any supporting files"
node_kind = "resource_input"
category = "Input"
executable_path = "ghcr.io/modtim/resource-input:latest"
image_pull_policy = "IfNotPresent"
container_timeout = 300
container_gpu_required = False
input_schema = {"input_type": "NONE", "input_variables": "NONE"}
output_schema = {"output_type": "resource", "file_mode": "single", "output_variables": "resource_name,resource_path"}
configuration_schema = {"extra_variables": {
    "resource_name": {
        "type": "string",
        "default": "",
        "description": "Name to access this resource in your script (e.g., 'my_shader', 'config', 'base_material'). Access via resources['my_shader']"
    }
}}
metadata = {"accepts_metadata": False, "metadata_required": False}
batch_mode = "single"
cost_in_credits = 0
tenant_id = None
# --- END METADATA BLOCK ---
