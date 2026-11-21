# EXAMPLE DAG showing how to use @track_resources decorator
# This demonstrates automatic resource tracking with multi-vendor GPU support

# --- METADATA BLOCK ---
name = "Example Tracked Process"
description = "Example showing resource tracking usage"
node_kind = "example_tracked_process"
category = "Example"
executable_path = "none"
input_schema = {"input_type": "any", "file_mode": "single", "input_variables": "NONE"}
output_schema = {"output_type": "any", "file_mode": "single", "paired": False, "output_variables": "NONE"}
configuration_schema = {"extra_variables": "NONE"}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
cost_in_credits = 1
tenant_id = None
# --- END METADATA BLOCK ---
