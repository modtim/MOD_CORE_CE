# --- METADATA BLOCK ---
name = "Script Input"
description = "Input - Code/script input for GIMP Python-Fu, Blender Python, or other scripting"
node_kind = "script_input"
category = "Input"
executable_path = "ghcr.io/modtim/script-input:latest"
image_pull_policy = "IfNotPresent"
container_timeout = 300
container_gpu_required = False
input_schema = {"input_type": "NONE", "input_variables": "NONE"}
output_schema = {"output_type": "script", "file_mode": "single", "output_variables": "script_content"}
configuration_schema = {"extra_variables": {
    "script_content": {
        "type": "code",
        "language": "python",
        "default": "# Enter your script here\n",
        "description": "Enter your Python script code. This will be passed to script-enabled nodes like GIMP Script or Blender Script."
    }
}}
metadata = {"accepts_metadata": False, "metadata_required": False}
batch_mode = "single"
cost_in_credits = 0
tenant_id = None
# --- END METADATA BLOCK ---
