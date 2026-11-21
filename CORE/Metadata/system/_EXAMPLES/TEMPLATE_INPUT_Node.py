# --- METADATA BLOCK ---
# TEMPLATE: Copy this file and replace all values marked with [REPLACE]
name = "[REPLACE] Your Input Name"  # e.g., "Document Input", "CAD Input"
description = "Input - [REPLACE] description of what this input handles"
node_kind = "[REPLACE]_input"  # e.g., "document_input", "cad_input" (lowercase, underscores)
category = "Input"
executable_path = "none"
image_pull_policy = "IfNotPresent"
container_timeout = 300
container_gpu_required = False
input_schema = {"input_type": "NONE", "input_variables": "NONE"}
output_schema = {"output_type": "[REPLACE]", "file_mode": "both", "output_variables": "NONE"}  # e.g., "document", "cad"
configuration_schema = {"extra_variables": "NONE"}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"  # Can process multiple files independently
cost_in_credits = 0  # Input DAGs are typically free
tenant_id = None
# --- END METADATA BLOCK ---
