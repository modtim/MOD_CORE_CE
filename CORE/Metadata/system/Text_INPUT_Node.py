# --- METADATA BLOCK ---
name = "Text Input"
description = "Input - Direct text input for prompts, descriptions, or any text content"
node_kind = "text_input"
category = "Input"
executable_path = "ghcr.io/modtim/text-input:latest"
image_pull_policy = "IfNotPresent"
container_timeout = 300
container_gpu_required = False
input_schema = {"input_type": "NONE", "input_variables": "NONE"}
output_schema = {"output_type": "text", "file_mode": "single", "output_variables": "text_content"}
configuration_schema = {"extra_variables": {
    "text_content": {"type": "string", "default": "", "description": "Enter your text content here. This can be a prompt, description, or any text you want to use in your workflow"}
}}
metadata = {"accepts_metadata": False, "metadata_required": False}
batch_mode = "single"
cost_in_credits = 0
tenant_id = None
# --- END METADATA BLOCK ---
